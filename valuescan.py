"""
valuescan.py - exact-value memory scan with interactive refinement.

For fields whose value you can READ off the screen: the stage number in
Combo Challenge, a round counter, a score. Type the number you see, change
it in the game, type the new one, and repeat until a handful of addresses
remain. Addresses inside DOA6LR.exe are printed as exe+offset, ready for
layout.json.

    python valuescan.py                 # u32 (also try --dtype u16 / u8)
    value> 3                            # first scan: every u32 equal to 3
    value> 4                            # went to stage 4: keep those now 4
    value> =                            # keep the ones that did NOT change
    value> !                            # keep the ones that DID change
    value> list                         # print candidates
    value> save                         # -> valuescan.json
    value> q

Run as Administrator, with the game running.
"""
import argparse
import json
import sys
import time

import numpy as np

from memlib import Process, find_pid
from scanner import DTYPES, read_values, snapshot_regions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default="DOA6LR.exe")
    ap.add_argument("--dtype", default="u32", choices=list(DTYPES))
    ap.add_argument("--max", type=int, default=40, help="candidates printed by 'list'")
    args = ap.parse_args()

    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running"); sys.exit(1)
    proc = Process(pid)
    dt = DTYPES[args.dtype]
    item = np.dtype(dt).itemsize
    mods = [(m[0], m[1], m[2]) for m in proc.modules()]

    def where(addr):
        for name, base, size in mods:
            if base <= addr < base + size:
                return f"{name}+0x{addr - base:X}"
        return "heap"

    addrs = None          # np.int64 array of candidate addresses
    last = None           # their values at the previous step
    history = []

    def show():
        if addrs is None:
            print("  no scan yet"); return
        print(f"  {len(addrs)} candidate(s)")
        vals = read_values(proc, addrs[:args.max], dt) if len(addrs) else []
        for a, v in zip(addrs[:args.max], vals):
            print(f"    0x{int(a):X}  = {v}   ({where(int(a))})")
        if len(addrs) > args.max:
            print(f"    ... {len(addrs) - args.max} more")

    print(f"scanning {args.process} (pid {pid}) as {args.dtype}. "
          "Type the value you see in the game; '=' unchanged, '!' changed, "
          "'list', 'undo', 'save', 'q'.")
    while True:
        try:
            cmd = input("value> ").strip()
        except EOFError:
            break
        if not cmd:
            continue
        if cmd in ("q", "quit", "exit"):
            break
        if cmd == "list":
            show(); continue
        if cmd == "undo":
            if history:
                addrs, last = history.pop()
                print(f"  back to {len(addrs)} candidate(s)")
            continue
        if cmd == "save":
            out = [{"addr": f"0x{int(a):X}", "where": where(int(a))} for a in (addrs if addrs is not None else [])]
            with open("valuescan.json", "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=1)
            print(f"  saved {len(out)} to valuescan.json"); continue

        t0 = time.perf_counter()
        if cmd in ("=", "!"):
            if addrs is None or last is None:
                print("  scan a value first"); continue
            now = read_values(proc, addrs, dt)
            keep = (now == last) if cmd == "=" else (now != last)
            history.append((addrs, last))
            addrs, last = addrs[keep], now[keep]
        else:
            try:
                target = dt(float(cmd)) if args.dtype == "f32" else dt(int(cmd, 0))
            except (ValueError, OverflowError):
                print("  not a number"); continue
            if addrs is None:
                found = []
                for (base, size), arr in snapshot_regions(proc, dt).items():
                    hits = np.flatnonzero(arr == target)
                    if len(hits):
                        found.append(base + hits.astype(np.int64) * item)
                addrs = np.concatenate(found) if found else np.zeros(0, dtype=np.int64)
                addrs.sort()
                last = np.full(len(addrs), target, dtype=dt)
            else:
                now = read_values(proc, addrs, dt)
                keep = now == target
                history.append((addrs, last))
                addrs, last = addrs[keep], now[keep]
        print(f"  {len(addrs)} candidate(s)  ({time.perf_counter() - t0:.1f} s)")
        if 0 < len(addrs) <= 12:
            show()


if __name__ == "__main__":
    main()
