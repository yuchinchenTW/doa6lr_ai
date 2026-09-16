"""
fields.py - DOA6LR field table and anchor resolution.

DOA5LR had WAZAAAAA's toolbox: one AOB signature (AF 47 E9 42) and a table of
fixed offsets that still worked on the 2025 build. DOA6 Last Round (June 2026,
DOA6LR.exe, 64-bit) has no equivalent - that signature is absent from the
process, every published DOA6 cheat table targets the 2019 DOA6.exe with
static offsets, and none of them expose frame data anyway. So the table lives
in layout.json and is filled in as autoscan.py / scanner.py find each field.

layout.json shape:

    {
      "process": "DOA6LR.exe",
      "anchors": {
        "state": {"kind": "pointer", "module_offset": "0x7F55158",
                  "offsets": ["0x8", "0x3A8", "0x30"]},
        "pos":   {"kind": "pointer", "module_offset": "0x5DAFD10",
                  "offsets": ["0x0"]}
      },
      "players": {"P1": {"state": "0x0", "pos": "0x0"},
                  "P2": {"state": "0x10DFA0", "pos": "0x60"}},
      "fields":  {"CurrentMove": ["0x68", "u16", "state"], ...},
      "globals": {}
    }

Several anchors, because DOA6LR splits a character over several objects
(as DOA5 did): the per-player state block found by autoscan/probe, and the
position object with its own static root. A field names the anchor it is
relative to (third element, default "state"); a player entry gives that
side's offset from each anchor.

anchor kinds:
    static    module_offset                 anchor = exe base + module_offset
    pointer   module_offset, offsets[]      walk the chain; last hop is not
                                            dereferenced
    aob       signature (hex), add          first occurrence of the bytes + add
    absolute  address                       for a single session only

Field names are "<side>_<field>" for per-player fields (P1_CurrentMove) and
whatever key is used for globals (PX_Distance), same as the DOA5 tooling, so
holdbot-style code ports across without renaming.

    python fields.py            resolve the anchor and dump every field
    python fields.py --watch    print on change
"""

import argparse
import ctypes
import json
import os
import sys
import time

from memlib import (MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS,
                    MEMORY_BASIC_INFORMATION, Process, find_pid, k32)

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUT_FILE = os.path.join(HERE, "layout.json")

# Carried over from DOA5LR as the working hypothesis for what the equivalent
# DOA6 fields will look like. Verify each against the game before trusting it:
# the enum values are Team Ninja's and the engine lineage is the same, but
# nothing here has been confirmed on DOA6LR yet.
STRIKE_TYPE = {0: "high punch", 1: "high kick", 2: "mid punch",
               3: "mid kick", 4: "low punch", 5: "low kick",
               255: "not a strike"}
HIGH_MID_LOW = {1: "high", 2: "medium", 3: "low", 4: "ground",
                255: "multi-height"}
MOVE_TYPE = {0: "movement/idle", 2: "strike", 3: "strike",
             4: "throw active", 5: "whiffed hold", 6: "hold active",
             7: "blockstun", 8: "critical stun", 9: "hitstun/juggle",
             10: "being thrown", 11: "being held", 12: "down",
             13: "movement/special", 14: "ground attack",
             15: "hit on the ground", 16: "throw startup"}

READERS = {"u8": "u8", "u16": "u16", "u32": "u32", "i32": "i32",
           "u64": "u64", "f32": "f32", "f64": "f64"}


def _int(x):
    return int(x, 0) if isinstance(x, str) else int(x)


def load_layout(path=LAYOUT_FILE):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    anchors = raw.get("anchors") or ({"state": raw["anchor"]}
                                     if raw.get("anchor") else {})

    def spec(v):
        return (_int(v[0]), v[1], v[2] if len(v) > 2 else "state")

    players = {}
    for side, v in raw.get("players", {}).items():
        players[side] = ({k: _int(o) for k, o in v.items()}
                         if isinstance(v, dict) else {"state": _int(v)})
    return {"process": raw.get("process", "DOA6LR.exe"),
            "anchors": anchors,
            "players": players,
            "fields": {k: spec(v) for k, v in raw.get("fields", {}).items()},
            "globals": {k: spec(v) for k, v in raw.get("globals", {}).items()},
            "notes": raw.get("notes", {})}


def scan_signature(proc, sig, want_all=False):
    """First occurrence of `sig` in committed readable memory, scanning up.

    Same walk as the DOA5 tooling so that, if a DOA6 signature is ever
    chosen, "first hit" means the same thing in both projects. Reads in
    64 MB pieces with an overlap of len(sig)-1 so a match straddling a piece
    boundary is not missed.
    """
    hits = []
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()
    step = 64 << 20
    while addr <= proc.addr_limit:
        if not k32.VirtualQueryEx(proc.h, ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if size == 0:
            break
        readable = (mbi.State == MEM_COMMIT
                    and not (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)))
        if readable and size >= len(sig):
            off = 0
            while off < size:
                n = min(step, size - off)
                extra = len(sig) - 1 if off + n < size else 0
                buf = proc.read_tolerant(base + off, n + extra)
                start = 0
                while True:
                    i = buf.find(sig, start)
                    if i < 0 or i >= n:
                        break
                    hits.append(base + off + i)
                    if not want_all:
                        return hits[0], hits
                    start = i + 1
                off += n
        addr = base + size
    return (hits[0] if hits else None), hits


def locate(proc, layout=None, name="state"):
    """Resolve one named anchor to an absolute address, or None."""
    layout = layout or load_layout()
    if not layout or name not in layout.get("anchors", {}):
        return None
    a = layout["anchors"][name]
    kind = a.get("kind")
    if kind == "absolute":
        return _int(a["address"])
    if kind == "aob":
        sig = bytes.fromhex(a["signature"].replace(" ", ""))
        hit, _ = scan_signature(proc, sig)
        return None if hit is None else hit + _int(a.get("add", 0))
    mod = proc.module(layout["process"])
    if not mod:
        return None
    base = mod[1] + _int(a["module_offset"])
    if kind == "static":
        return base
    if kind == "pointer":
        addr = base
        for off in [_int(o) for o in a.get("offsets", [])]:
            v = proc.ptr(addr)
            if not v:
                return None
            addr = v + off
        return addr
    raise ValueError(f"unknown anchor kind {kind!r}")


def locate_all(proc, layout=None):
    """{anchor name: address}; an anchor that fails to resolve is None."""
    layout = layout or load_layout()
    if not layout:
        return {}
    return {n: locate(proc, layout, n) for n in layout["anchors"]}


def field_address(layout, anchors, name):
    """anchors: {name: address} from locate_all (a bare int is taken as the
    "state" anchor, which keeps the DOA5-style call sites working)."""
    if not isinstance(anchors, dict):
        anchors = {"state": anchors}
    if name in layout["globals"]:
        off, kind, an = layout["globals"][name]
        return anchors[an] + off, kind
    side, _, fname = name.partition("_")
    if side in layout["players"] and fname in layout["fields"]:
        off, kind, an = layout["fields"][fname]
        # "<anchor>:<side>" is a per-side anchor (its own pointer chain); it
        # wins over the shared anchor plus a per-side offset. DOA6LR's two
        # player blocks are not a fixed distance apart, so P2 needs this.
        base = anchors.get(f"{an}:{side}", anchors[an])
        return base + layout["players"][side].get(an, 0) + off, kind
    raise KeyError(f"unknown field {name!r}")


def read_field(proc, anchors, name, layout=None):
    layout = layout or load_layout()
    addr, kind = field_address(layout, anchors, name)
    return getattr(proc, READERS[kind])(addr)


def all_names(layout):
    names = []
    for side in layout["players"]:
        for f in layout["fields"]:
            names.append(f"{side}_{f}")
    names.extend(layout["globals"])
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--process", default=None)
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()

    layout = load_layout()
    if layout is None:
        print(f"no {LAYOUT_FILE}. Run autoscan.py to find the first fields, "
              f"then write the layout.")
        sys.exit(1)
    name = args.process or layout["process"]
    pid = find_pid(name)
    if pid is None:
        print(f"{name} is not running.")
        sys.exit(1)
    proc = Process(pid)
    t = time.perf_counter()
    anchors = locate_all(proc, layout)
    bad = [n for n, a in anchors.items() if a is None]
    if bad:
        print(f"anchor(s) did not resolve: {', '.join(bad)}; the layout is "
              f"stale or incomplete.")
        sys.exit(2)
    for n, a in anchors.items():
        print(f"anchor {n:<8} 0x{a:012X}")
    print(f"  ({time.perf_counter() - t:.2f}s)\n")
    names = all_names(layout)
    last = {}
    try:
        while True:
            for n in names:
                v = read_field(proc, anchors, n, layout)
                if not args.watch or last.get(n) != v:
                    addr, kind = field_address(layout, anchors, n)
                    extra = ""
                    if n.endswith("StrikeType") and v in STRIKE_TYPE:
                        extra = f"   <- {STRIKE_TYPE[v]}"
                    if isinstance(v, float):
                        v = round(v, 4)
                    print(f"  {n:<22} 0x{addr:012X} {kind:>4} "
                          f"{str(v):>12}{extra}")
                    last[n] = v
            if not args.watch:
                break
            time.sleep(1 / 120)
    except KeyboardInterrupt:
        pass
    proc.close()


if __name__ == "__main__":
    main()
