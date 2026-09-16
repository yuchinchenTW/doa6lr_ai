"""
memlib.py - minimal Windows process-memory toolkit.

Handles both a 32-bit (WOW64) target such as DOA5LR's game.exe and a native
64-bit one such as DOA6LR.exe. ReadProcessMemory works across the boundary;
the only things that change are the address range worth scanning and the
pointer width, both of which Process picks up from the target at attach time.

DOA6LR specifics that shaped the region walker: the process commits ~8.5 GB
of private writable memory, 3.3 GB of it PAGE_WRITECOMBINE (GPU upload heaps
that never hold gameplay state) and a single 4.2 GB arena of which ~1 GB is
non-zero. regions() therefore skips write-combine pages by default and can
split big regions into fixed-size chunks so a caller never has to allocate a
4 GB buffer to look at one region.
"""

import ctypes
import ctypes.wintypes as wt
import struct
from dataclasses import dataclass
from typing import Iterator, Optional

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- constants

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_RW = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
              | PROCESS_VM_WRITE | PROCESS_VM_OPERATION)

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
PAGE_WRITECOMBINE = 0x400

WRITABLE = (PAGE_READWRITE | PAGE_WRITECOPY
            | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ------------------------------------------------------------------ structs

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", ctypes.c_void_p),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * 260),
    ]


# ------------------------------------------------------------- api bindings

k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = ctypes.c_void_p

k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.CloseHandle.restype = wt.BOOL

k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wt.BOOL

k32.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wt.BOOL

k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t

k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p

k32.IsWow64Process.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.BOOL)]
k32.IsWow64Process.restype = wt.BOOL


# ------------------------------------------------------------------- helpers

def list_processes():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed")
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        if not k32.Process32FirstW(snap, ctypes.byref(pe)):
            return out
        while True:
            out.append((pe.th32ProcessID, pe.szExeFile))
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def find_pid(name: str) -> Optional[int]:
    name = name.lower()
    for pid, exe in list_processes():
        if exe.lower() == name:
            return pid
    return None


@dataclass
class Region:
    base: int
    size: int
    protect: int
    type: int
    alloc_base: int = 0

    @property
    def end(self) -> int:
        return self.base + self.size


class Process:
    """Handle to a target process. Usable as a context manager."""

    def __init__(self, pid: int):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_RW, False, pid)
        if not self.h:
            err = ctypes.get_last_error()
            hint = "  (run this shell as Administrator)" if err == 5 else ""
            raise OSError(f"OpenProcess({pid}) failed, error {err}{hint}")
        self.is_32bit = self._detect_wow64()
        # A 32-bit target can only address the low 2 GB worth scanning; a
        # 64-bit one needs the full user-mode range or regions() finds nothing.
        self.addr_limit = 0x7FFFFFFF if self.is_32bit else 0x7FFFFFFFFFFF

    @property
    def ptr_size(self) -> int:
        return 4 if self.is_32bit else 8

    def _detect_wow64(self) -> bool:
        """True when the target is a 32-bit process. Assumes a 64-bit OS."""
        flag = wt.BOOL()
        if not k32.IsWow64Process(self.h, ctypes.byref(flag)):
            return True
        return bool(flag.value)

    @classmethod
    def by_name(cls, name: str) -> "Process":
        pid = find_pid(name)
        if pid is None:
            raise LookupError(f"process {name!r} is not running")
        return cls(pid)

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- raw io ------------------------------------------------------------

    def read(self, addr: int, size: int) -> Optional[bytes]:
        buf = (ctypes.c_ubyte * size)()
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr),
                                   ctypes.byref(buf), size, ctypes.byref(got))
        if not ok or got.value != size:
            return None
        return bytes(buf)

    def read_into(self, addr: int, buf: bytearray) -> int:
        """Read len(buf) bytes into a pre-allocated bytearray. Returns bytes read."""
        n = len(buf)
        cbuf = (ctypes.c_ubyte * n).from_buffer(buf)
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr),
                                   ctypes.byref(cbuf), n, ctypes.byref(got))
        return got.value if ok else 0

    def read_tolerant(self, addr: int, size: int, page: int = 0x1000) -> bytes:
        """Read a span, substituting zeros for any page that refuses to read.

        A region can change protection between VirtualQueryEx and the read, so
        a whole-region read failing is normal rather than exceptional.
        """
        whole = self.read(addr, size)
        if whole is not None:
            return whole
        out = bytearray(size)
        off = 0
        while off < size:
            n = min(page, size - off)
            chunk = self.read(addr + off, n)
            if chunk:
                out[off:off + n] = chunk
            off += n
        return bytes(out)

    def write(self, addr: int, data: bytes) -> bool:
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        put = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr),
                                    ctypes.byref(buf), len(data),
                                    ctypes.byref(put))
        return bool(ok) and put.value == len(data)

    # -- typed reads -------------------------------------------------------

    def _num(self, addr, fmt, size):
        b = self.read(addr, size)
        return struct.unpack(fmt, b)[0] if b else None

    def u8(self, a):    return self._num(a, "<B", 1)
    def u16(self, a):   return self._num(a, "<H", 2)
    def u32(self, a):   return self._num(a, "<I", 4)
    def i32(self, a):   return self._num(a, "<i", 4)
    def f32(self, a):   return self._num(a, "<f", 4)
    def u64(self, a):   return self._num(a, "<Q", 8)
    def i64(self, a):   return self._num(a, "<q", 8)
    def f64(self, a):   return self._num(a, "<d", 8)
    def ptr32(self, a): return self._num(a, "<I", 4)
    def ptr64(self, a): return self._num(a, "<Q", 8)

    def ptr(self, a):
        """Read a pointer of the target's own width."""
        return self.ptr32(a) if self.is_32bit else self.ptr64(a)

    def follow(self, base: int, offsets: list) -> Optional[int]:
        """Walk a pointer chain: base+off0 -> deref -> +off1 -> ...

        Uses the target's pointer width, so the same call works against a
        32-bit and a 64-bit game."""
        addr = base + offsets[0]
        for off in offsets[1:]:
            nxt = self.ptr(addr)
            if not nxt:
                return None
            addr = nxt + off
        return addr

    # -- layout ------------------------------------------------------------

    def stack_allocations(self) -> set:
        """Allocation bases that contain a guard page.

        Thread stacks are the only thing that reserves a block and protects the
        low end with PAGE_GUARD, so this identifies them without touching the
        TEB. Worth excluding from scans: a stack slot written by the same call
        path every frame changes while an action runs and is restored when it
        ends, which is exactly the pattern a state-field search looks for, so
        stacks otherwise flood the results with false positives.
        """
        bases = set()
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < self.addr_limit:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            size = mbi.RegionSize
            if size == 0:
                break
            if mbi.Protect & PAGE_GUARD:
                bases.add(mbi.AllocationBase or 0)
            addr = (mbi.BaseAddress or 0) + size
        return bases

    def regions(self, writable_only=True, private_only=True,
                max_region=1 << 40, exclude_stacks=True,
                exclude_writecombine=True, chunk=0) -> Iterator[Region]:
        """Yield committed regions worth scanning.

        exclude_writecombine drops PAGE_WRITECOMBINE pages. On DOA6LR that is
        3.3 GB of GPU upload heaps: vertex and constant buffers the renderer
        streams every frame, which change constantly and hold no gameplay
        state. Keeping them triples scan time and floods any "changed" filter.

        chunk > 0 splits every region into pieces of at most that many bytes.
        The 4.2 GB arena DOA6LR keeps would otherwise force a single 4 GB read
        buffer per snapshot; with chunking the caller decides the peak, and
        can drop chunks that are entirely zero (most of that arena is).

        max_region stays a sanity guard rather than a budget: a region that is
        never read can never produce a candidate.
        """
        skip = self.stack_allocations() if exclude_stacks else set()
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        limit = self.addr_limit
        while addr < limit:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            alloc = mbi.AllocationBase or 0
            if size == 0:
                break
            ok = mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
            if ok and writable_only:
                ok = bool(mbi.Protect & WRITABLE)
            if ok and private_only:
                ok = mbi.Type == MEM_PRIVATE
            if ok and exclude_writecombine:
                ok = not (mbi.Protect & PAGE_WRITECOMBINE)
            if ok and alloc in skip:
                ok = False
            if ok and size <= max_region:
                if chunk and size > chunk:
                    off = 0
                    while off < size:
                        n = min(chunk, size - off)
                        yield Region(base + off, n, mbi.Protect, mbi.Type,
                                     alloc)
                        off += n
                else:
                    yield Region(base, size, mbi.Protect, mbi.Type, alloc)
            addr = base + size

    def modules(self) -> list:
        snap = k32.CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap == INVALID_HANDLE_VALUE:
            return []
        out = []
        try:
            me = MODULEENTRY32W()
            me.dwSize = ctypes.sizeof(me)
            if not k32.Module32FirstW(snap, ctypes.byref(me)):
                return out
            while True:
                base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value or 0
                out.append((me.szModule, base, me.modBaseSize))
                if not k32.Module32NextW(snap, ctypes.byref(me)):
                    break
        finally:
            k32.CloseHandle(snap)
        return out

    def module(self, name: str):
        name = name.lower()
        for m in self.modules():
            if m[0].lower() == name:
                return m
        return None


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "DOA6LR.exe"
    pid = find_pid(target)
    if pid is None:
        print(f"{target} not running. Candidate processes:")
        for p, e in list_processes():
            if "game" in e.lower() or "doa" in e.lower():
                print(f"  {p:>8}  {e}")
        sys.exit(1)
    with Process(pid) as p:
        print(f"{target}  pid={pid}  "
              f"{'32-bit' if p.is_32bit else '64-bit'}")
        m = p.module(target)
        if m:
            print(f"  module base 0x{m[1]:012X}  size 0x{m[2]:X}")
        regs = list(p.regions())
        total = sum(r.size for r in regs)
        print(f"  {len(regs)} private writable regions, "
              f"{total / 1048576:.1f} MB scannable "
              f"(write-combine excluded)")
        wc = list(p.regions(exclude_writecombine=False))
        wc_total = sum(r.size for r in wc) - total
        print(f"  {wc_total / 1048576:.1f} MB of write-combine (GPU upload) "
              f"memory skipped")
