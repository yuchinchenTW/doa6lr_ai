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
import bisect
import json
import os
import shutil
import math
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


OBJ = 0x910          # through StrikeType at +0x904, the last field we use


def find_state_objects(proc, want_char=None, limit=64):
    """Heap objects matching the player-state signature, confirmed live.

    Two things went wrong before this. Checking six fields per candidate with
    a read each, and stopping at the first 256 matches, filled the list with
    blank pages from the first region: character 0, health 2, move 0 all pass
    a range check. Then 24k matches survived a tighter signature and none of
    them ticked, because "ticked" meant one named field, the per-move frame
    counter, and a patch that moved the static data can move a field too.

    So: the whole signature is compared in numpy across each region, using
    every field layout.json knows including the ones far into the object, and
    liveness is "any byte of the object changed", not one field.
    """
    windows = []                      # (address, the object as it was)
    for r in proc.regions(writable_only=True, private_only=True, chunk=1 << 24):
        buf = proc.read(r.base, r.size)
        if not buf or len(buf) < OBJ + 8:
            continue
        n = (len(buf) - OBJ) // 8
        if n <= 0:
            continue

        def fld(off, dt):
            step = 8 // np.dtype(dt).itemsize
            return np.frombuffer(buf, dt, offset=off, count=n * step)[::step]

        char = fld(0x14, np.uint32)
        move = fld(0x68, np.uint16)
        kind = fld(0x108, np.uint8)
        phase = fld(0x128, np.uint16)
        frame = fld(0x174, np.uint16)
        mtype = fld(0x578, np.uint8)
        hp = fld(0x580, np.uint16)
        anim = fld(0x630, np.uint32)
        hml = fld(0x8EC, np.uint32)
        strike = fld(0x904, np.uint8)

        ok = ((char >= 1) & (char <= 120)        # a fighter id, never 0
              & (hp >= 1) & (hp <= 400)
              & (frame >= 1) & (frame <= 4000)   # the counter always ticks
              & (kind <= 40) & (phase <= 16) & (move <= 40000)
              & (mtype <= 40) & (strike <= 40)
              & (anim >= 1) & (anim <= 60000)    # an animation has a length
              & (hml <= 16))
        if want_char is not None:
            ok &= (char == want_char)
        for i in np.nonzero(ok)[0]:
            o = int(i) * 8
            windows.append((r.base + o, buf[o:o + OBJ]))
        if len(windows) > 200000:
            print("  (stopping the scan at 200k matches - tighten with --char)")
            break

    # Liveness: ANY byte of the object changed. Naming one field assumes that
    # field is still where it was, which is the thing a patch breaks.
    time.sleep(0.35)
    alive = []
    for addr, before in windows:
        now = proc.read(addr, OBJ)
        if now and now != before:
            alive.append(addr)
    return alive[:limit], len(windows), len(alive)


def heap_ranges(proc):
    """Private writable committed regions, sorted, for a fast "is this on the
    heap" test."""
    out = [(r.base, r.base + r.size)
           for r in proc.regions(writable_only=True, private_only=True)]
    out.sort()
    return out


def on_heap(ranges, addr):
    i = bisect.bisect_right(ranges, (addr, 1 << 63)) - 1
    return i >= 0 and ranges[i][0] <= addr < ranges[i][1]


def looks_like_pos(proc, addr, ranges):
    """The position object, which is not a state block and so can never match
    the state scan - the first version reported it NOT FOUND for that reason
    alone. It holds P1's vec4 at +0x5B0 and P2's at +0x610.

    Checking only "two finite points a sane distance apart" matched 13514
    slots, all of them inside loaded DLLs with coordinates of zero. A ring
    stands at roughly x 12000, y 2100, z 22000, so the magnitudes carry as
    much signal as the shape does, and the object lives on the heap rather
    than in a module image."""
    if not on_heap(ranges, addr):
        return False
    pts = []
    for off in (0x5B0, 0x610):
        raw = proc.read(addr + off, 12)
        if not raw:
            return False
        x, y, z = struct.unpack("<fff", raw)
        if not all(math.isfinite(c) for c in (x, y, z)):
            return False
        if not 100.0 < abs(x) < 1e6 or not 100.0 < abs(z) < 1e6:
            return False
        if not -2000.0 < y < 5e4:
            return False
        pts.append((x, y, z))
    return 5.0 < math.dist(pts[0], pts[1]) < 3000.0


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
    ap.add_argument("--char", type=int, default=None,
                    help="only accept state blocks whose character id is this "
                         "(35 for Minato, 21 Nyotengu, 30 Mai, 31 Kula). Use it "
                         "when the scan comes back with implausible ids.")
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
    states, raw_n, n_alive = find_state_objects(proc, args.char)
    if not states:
        print(f"no live state blocks found: {raw_n} matched the signature, "
              f"{n_alive} of them changed at all over 0.35 s.")
        if raw_n and not n_alive:
            print("Nothing in memory moved, so the game is not running a live "
                  "match: check it is not paused, not in a menu, and not on "
                  "the character select.")
        else:
            print("Try --char with the fighter id you are playing.")
        return 1
    print(f"  {len(states)} shown; {n_alive} live out of {raw_n} matching "
          f"the signature:")
    for a in states[:8]:
        print(f"    0x{a:X}  char={proc.u32(a + 0x14):<4} "
              f"hp={proc.u16(a + 0x580):<4} move={proc.u16(a + 0x68)}")
    want = set(states)

    ranges = heap_ranges(proc)
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
        is_pos = name.split(":")[0] == "pos"
        hits = []
        for off, val in slots:
            addr = walk(proc, mod[1] + off, offs)
            if addr is None:
                continue
            if (looks_like_pos(proc, addr, ranges) if is_pos
                    else (addr in want)):
                hits.append((off, addr))
        if hits:
            found[name] = hits
            old = _int(a["module_offset"])
            for off, addr in hits[:4]:
                mark = "  (unchanged)" if off == old else ""
                extra = ""
                if is_pos:
                    a1 = [proc.f32(addr + 0x5B0 + 4 * k) for k in range(3)]
                    a2 = [proc.f32(addr + 0x610 + 4 * k) for k in range(3)]
                    extra = (f"  P1 ({a1[0]:.0f}, {a1[1]:.0f}, {a1[2]:.0f}) "
                             f"P2 ({a2[0]:.0f}, {a2[1]:.0f}, {a2[2]:.0f}) "
                             f"apart {math.dist(a1, a2):.0f}")
                print(f"  {name}: exe+0x{off:X} -> 0x{addr:X}{mark}{extra}")
            if len(hits) > 4:
                print(f"  {name}: and {len(hits) - 4} more")
        else:
            what = ("a position object (two finite points a sane distance "
                    "apart)" if is_pos else "any of the state blocks above")
            print(f"  {name}: NOT FOUND. No slot in the image reaches {what} "
                  f"through its stored offsets {a.get('offsets')}, so the tail "
                  f"of this chain moved as well - that one needs "
                  f"pointerscan.py.")

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
