"""
relocate.py - repair layout.json after the game patches.

A game update moves the executable's static data around, so the module offset
at the root of every pointer chain goes stale and holdbot prints

    anchors did not resolve: {'state': None, 'state:P2': None, ...}

The chain's TAIL almost never moves: the struct offsets (+0x8, +0x3A8, +0x30
for the state object) are the game's own field layout, and a patch that
reshuffled those would break far more than the root. So this only re-finds the
root, in two steps:

  1. Scan the heap for objects that look like a player state block: the field
     signature from layout.json, checked against ranges nothing else satisfies
     (a health in 0..400 at +0x580 AND a character id at +0x14 AND a small
     MoveKind at +0x108 AND a Phase under 16 AND a frame counter under 4000).
     Confirm each by sampling twice: the frame counter must advance.
  2. Walk every aligned slot of DOA6LR.exe's image, apply the anchor's stored
     tail offsets to whatever pointer the slot holds, and keep the slots that
     land on one of those objects.

    python relocate.py                 report, change nothing
    python relocate.py --write         update layout.json (a .bak is kept)

A match has to be running: in the character select or a menu the state objects
do not exist and there is nothing to find.
"""

import argparse
import json
import os
import shutil
import struct
import sys
import time

import numpy as np

from fields import load_layout, _int
from memlib import Process, find_pid

LAYOUT = "layout.json"
# what a player state block has to look like, read at the field offsets
# layout.json already stores. Ranges are deliberately tight.
CHECKS = [
    ("CurrentCharacter", 0x14, "u32", 0, 200),
    ("CurrentMove", 0x68, "u16", 0, 40000),
    ("MoveKind", 0x108, "u8", 0, 40),
    ("Phase", 0x128, "u16", 0, 16),
    ("CurrentMoveFrame", 0x174, "u16", 0, 4000),
    ("CurrentHealth", 0x580, "u16", 1, 400),
]
SIZES = {"u8": 1, "u16": 2, "u32": 4}
FMTS = {"u8": "<B", "u16": "<H", "u32": "<I"}


def looks_like_state(proc, addr):
    for _name, off, kind, lo, hi in CHECKS:
        raw = proc.read(addr + off, SIZES[kind])
        if not raw:
            return False
        v = struct.unpack(FMTS[kind], raw)[0]
        if not lo <= v <= hi:
            return False
    return True


def find_state_objects(proc, limit=64):
    """Heap objects matching the state signature, confirmed by a live frame
    counter. Scans on the health field first: a u16 in 1..400 at a 16-byte
    aligned address is common, but the five other fields together are not."""
    hits = []
    seen = set()
    for r in proc.regions(writable_only=True, private_only=True, chunk=1 << 24):
        buf = proc.read(r.base, r.size)
        if not buf or len(buf) < 0x1000:
            continue
        arr = np.frombuffer(buf, dtype=np.uint16,
                            count=(len(buf) // 2))
        # candidate health values, then step back to the object base
        idx = np.nonzero((arr >= 1) & (arr <= 400))[0]
        for i in idx:
            base = r.base + int(i) * 2 - 0x580
            if base <= 0 or base % 8 or base in seen:
                continue
            seen.add(base)
            if looks_like_state(proc, base):
                hits.append(base)
                if len(hits) >= limit * 4:
                    break
        if len(hits) >= limit * 4:
            break

    # a real state block ticks: the frame counter advances while the game runs
    alive = []
    first = {a: proc.u16(a + 0x174) for a in hits}
    time.sleep(0.35)
    for a in hits:
        if proc.u16(a + 0x174) != first.get(a):
            alive.append(a)
    return (alive or hits)[:limit]


def module_slots(proc, mod):
    """Every 8-byte aligned slot in the executable image that holds something
    that could be a pointer, as (module offset, value)."""
    base, size = mod[1], mod[2]
    out = []
    step = 1 << 24
    for off in range(0, size, step):
        n = min(step, size - off)
        buf = proc.read_tolerant(base + off, n)
        if not buf:
            continue
        n8 = len(buf) // 8
        vals = np.frombuffer(buf, dtype=np.uint64, count=n8)
        keep = np.nonzero((vals > 0x10000) & (vals < (1 << 48)))[0]
        for i in keep:
            out.append((off + int(i) * 8, int(vals[i])))
    return out


def walk(proc, start, offsets):
    """The anchor's own rule: dereference at each hop, add the offset, and do
    not dereference the last one."""
    addr = start
    for off in offsets:
        v = proc.ptr(addr)
        if not v:
            return None
        addr = v + off
    return addr


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="update layout.json (the old one is kept as .bak)")
    ap.add_argument("--anchor", action="append", default=None,
                    help="only this anchor; may be repeated")
    args = ap.parse_args()

    layout = load_layout()
    if not layout:
        print("no layout.json")
        return 1
    raw = json.load(open(LAYOUT, encoding="utf-8"))

    pid = find_pid(layout["process"])
    if not pid:
        print(f"{layout['process']} is not running")
        return 1
    proc = Process(pid)
    mod = proc.module(layout["process"])
    if not mod:
        print("could not find the module")
        return 1
    print(f"{layout['process']} base 0x{mod[1]:X} size 0x{mod[2]:X}")

    print("scanning the heap for player state blocks...")
    states = find_state_objects(proc)
    if not states:
        print("no state blocks found. Is a match actually running? "
              "In a menu or character select they do not exist yet.")
        return 1
    print(f"  {len(states)} candidate(s):")
    for a in states[:8]:
        print(f"    0x{a:X}  char={proc.u32(a + 0x14):<4} "
              f"hp={proc.u16(a + 0x580):<4} move={proc.u16(a + 0x68)}")
    want = set(states)

    print("walking the executable image...")
    slots = module_slots(proc, mod)
    print(f"  {len(slots)} pointer-shaped slots")

    names = args.anchor or [n for n, a in raw["anchors"].items()
                            if a.get("kind") == "pointer"]
    found = {}
    for name in names:
        a = raw["anchors"].get(name)
        if not a or a.get("kind") != "pointer":
            print(f"  {name}: not a pointer anchor, skipped")
            continue
        offs = [_int(o) for o in a.get("offsets", [])]
        hits = []
        for off, val in slots:
            addr = walk(proc, mod[1] + off, offs)
            if addr is not None and addr in want:
                hits.append((off, addr))
        if hits:
            found[name] = hits
            old = _int(a["module_offset"])
            for off, addr in hits[:4]:
                mark = "  (unchanged)" if off == old else ""
                print(f"  {name}: exe+0x{off:X} -> 0x{addr:X}{mark}")
            if len(hits) > 4:
                print(f"  {name}: and {len(hits) - 4} more")
        else:
            print(f"  {name}: NOT FOUND with its stored offsets {a.get('offsets')}. "
                  f"The tail of the chain moved too - that needs pointerscan.py "
                  f"against one of the addresses above.")

    if not args.write:
        print("\nnothing written. Re-run with --write to update layout.json.")
        return 0

    if not found:
        print("\nnothing to write.")
        return 1
    shutil.copyfile(LAYOUT, LAYOUT + ".bak")
    for name, hits in found.items():
        raw["anchors"][name]["module_offset"] = f"0x{hits[0][0]:X}"
    with open(LAYOUT, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=1)
        fh.write("\n")
    print(f"\nlayout.json updated ({LAYOUT}.bak kept):")
    for name, hits in found.items():
        print(f"  {name}: module_offset = 0x{hits[0][0]:X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
