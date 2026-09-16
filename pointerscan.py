"""
pointerscan.py - find a static pointer path to a dynamic address.

The struct address scanner.py finds is heap-allocated, so it changes every time
the game restarts. To make the bot survive a restart you need a path rooted in
the executable itself, for example:

    DOA6LR.exe + 0x05D6A2C8  ->  +0x38  ->  +0x1C8   ==  character struct

Pointer width follows the target: 8 bytes for DOA6LR.exe, 4 for DOA5LR.
A slot only counts as a pointer if its value lands inside a committed region
of the process, which is a far tighter test on a 64-bit address space than
any fixed numeric range.

Usage:
    python pointerscan.py 0x1AB2F3C4D40                 # search
    python pointerscan.py 0x1AB2F3C4D40 --depth 3 --maxoff 0x800
    python pointerscan.py --verify DOA6LR.exe+0x5D6A2C8,0x38,0x1C8

Scope: depth<=4 with capped branching. That covers the shallow paths games
normally use for a player array. If nothing is found, widen --maxoff first,
then --depth. Cheat Engine's pointer scanner is the heavier tool if this one
comes up empty.
"""

import argparse
import json
import sys

import numpy as np

from memlib import Process, find_pid


def parse_addr(s):
    return int(s, 16) if s.lower().startswith("0x") else int(s, 0)


class PointerMap:
    """Every aligned slot in the process that holds a plausible pointer."""

    def __init__(self, proc):
        self.proc = proc
        width = proc.ptr_size
        dt = np.uint32 if width == 4 else np.uint64
        # Committed regions of any kind: a pointer may target the image,
        # a mapped file or the heap, and a value that lands in none of them
        # is not a pointer.
        spans = sorted((r.base, r.end) for r in
                       proc.regions(writable_only=False, private_only=False,
                                    exclude_stacks=False,
                                    exclude_writecombine=False))
        lo = np.array([a for a, _ in spans], dtype=np.uint64)
        hi = np.array([b for _, b in spans], dtype=np.uint64)

        def plausible(arr):
            v = arr.astype(np.uint64)
            i = np.searchsorted(lo, v, side="right") - 1
            ok = i >= 0
            i = np.clip(i, 0, len(lo) - 1)
            return ok & (v < hi[i]) & (v > 0x10000)

        srcs, vals = [], []
        # include image and mapped regions: static globals live in .data
        for r in proc.regions(writable_only=True, private_only=False,
                              chunk=64 << 20):
            count = r.size // width
            if count == 0:
                continue
            buf = proc.read_tolerant(r.base, count * width)
            arr = np.frombuffer(buf, dtype=dt, count=count)
            if not arr.any():
                continue
            idx = np.flatnonzero(plausible(arr))
            if idx.size:
                srcs.append(r.base + idx.astype(np.uint64) * width)
                vals.append(arr[idx].astype(np.uint64))
        if not srcs:
            raise RuntimeError("no readable pointer slots found")
        self.src = np.concatenate(srcs)
        self.val = np.concatenate(vals)
        order = np.argsort(self.val, kind="stable")
        self.src = self.src[order]
        self.val = self.val[order]
        print(f"  pointer map: {len(self.val):,} slots "
              f"({(self.val.nbytes + self.src.nbytes) / 1048576:.0f} MB)")

    def pointing_into(self, target, maxoff):
        """Slots holding a value in [target-maxoff, target]; returns (src, off)."""
        lo = max(target - maxoff, 0)
        i = np.searchsorted(self.val, np.uint64(lo), "left")
        j = np.searchsorted(self.val, np.uint64(target), "right")
        if i >= j:
            return np.zeros(0, np.uint64), np.zeros(0, np.int64)
        return self.src[i:j], (target - self.val[i:j].astype(np.int64))


def scan(proc, target, depth, maxoff, branch, static_lo, static_hi):
    pmap = PointerMap(proc)
    results = []
    frontier = [(target, [])]
    seen = set()

    for level in range(depth):
        nxt = []
        for addr, path in frontier:
            srcs, offs = pmap.pointing_into(addr, maxoff)
            for s, off in zip(srcs.tolist(), offs.tolist()):
                if static_lo <= s < static_hi:
                    results.append({"module_offset": s - static_lo,
                                    "offsets": [off] + path,
                                    "depth": level + 1})
                elif s not in seen:
                    seen.add(s)
                    nxt.append((s, [off] + path))
        print(f"  depth {level + 1}: {len(results)} static hits, "
              f"{len(nxt)} nodes to expand")
        if len(nxt) > branch:
            nxt.sort(key=lambda n: n[1][0])   # prefer small offsets
            nxt = nxt[:branch]
        frontier = nxt
        if not frontier:
            break
    results.sort(key=lambda r: (r["depth"], sum(r["offsets"])))
    return results


def resolve(proc, mod_base, module_offset, offsets):
    addr = mod_base + module_offset
    for i, off in enumerate(offsets):
        v = proc.ptr(addr)
        if not v:
            return None
        addr = v + off
        if i == len(offsets) - 1:
            return addr
    return addr


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", nargs="?")
    ap.add_argument("--process", default="DOA6LR.exe")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--maxoff", type=lambda x: int(x, 0), default=0x600)
    ap.add_argument("--branch", type=int, default=4000)
    ap.add_argument("--verify", help="module+off,off,off  path to resolve")
    ap.add_argument("--out", default="pointer_paths.json")
    args = ap.parse_args()

    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running.")
        sys.exit(1)
    proc = Process(pid)
    mod = proc.module(args.process)
    if not mod:
        print(f"could not locate module {args.process}")
        sys.exit(1)
    _, mod_base, mod_size = mod
    print(f"{args.process} pid={pid} base=0x{mod_base:012X} size=0x{mod_size:X}")

    if args.verify:
        head, *rest = args.verify.split(",")
        mo = parse_addr(head.split("+", 1)[1]) if "+" in head else parse_addr(head)
        offs = [parse_addr(x) for x in rest]
        addr = resolve(proc, mod_base, mo, offs)
        print(f"  resolves to: "
              f"{'0x%012X' % addr if addr else 'NULL (path is stale)'}")
        proc.close()
        return

    if not args.target:
        ap.error("give a target address, or use --verify")
    target = parse_addr(args.target)
    print(f"searching for paths to 0x{target:012X} "
          f"(depth {args.depth}, maxoff 0x{args.maxoff:X})")

    hits = scan(proc, target, args.depth, args.maxoff, args.branch,
                mod_base, mod_base + mod_size)

    print(f"\n{len(hits)} static paths found; verifying the best 20:\n")
    good = []
    for h in hits[:20]:
        addr = resolve(proc, mod_base, h["module_offset"], h["offsets"])
        ok = addr == target
        chain = " -> ".join(f"+0x{o:X}" for o in h["offsets"])
        mark = "OK " if ok else "BAD"
        print(f"  [{mark}] {args.process}+0x{h['module_offset']:X}  {chain}")
        if ok:
            good.append(h)

    with open(args.out, "w") as f:
        json.dump({"process": args.process, "target": target,
                   "paths": good or hits[:50]}, f, indent=2)
    print(f"\nwrote {args.out}")
    print("Restart the game and re-verify before trusting a path. For DOA5LR\n"
          "itself prefer aob.py: a signature anchor survives patches, a pointer\n"
          "chain generally does not.")
    proc.close()


if __name__ == "__main__":
    main()
