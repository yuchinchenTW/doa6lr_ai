"""
watch.py - inspect a live struct once scanner.py has given you an address.

Three subcommands:

  fields    live-print named addresses, one line per change
  struct    live hexdump of a window, highlighting bytes that are moving
  classify  record one session per attack type, then report which offset in
            the struct separates them - this is what replaces a hand-built
            "move id -> attack type" table

classify is the important one. Example:

    python watch.py classify 0x1A2B3C40 --before 0x100 --after 0x400 \
           --labels high,midp,midk,low

  F1..F4 start/stop a recording session for the matching label. During the
  "high" session throw only high attacks, during "midp" only mid punches, and
  so on; neutral frames are shared by every session so they cancel out. F9
  prints the analysis, F7 saves it to attack_table.json.

  The report lists offsets whose values are exclusive to one label. An offset
  where every label owns a distinct value is the game's own attack-property
  field, and the values it prints are your lookup table.
"""

import argparse
import ctypes
import json
import sys
import time

import numpy as np

from memlib import Process, find_pid

u32dll = ctypes.WinDLL("user32", use_last_error=True)
VKF = {f"F{i}": 0x6F + i for i in range(1, 13)}


def beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


class Keys:
    def __init__(self):
        self.down = set()

    def pressed(self):
        hits = []
        for name, vk in VKF.items():
            is_down = bool(u32dll.GetAsyncKeyState(vk) & 0x8000)
            if is_down and name not in self.down:
                hits.append(name)
            if is_down:
                self.down.add(name)
            else:
                self.down.discard(name)
        return hits


def parse_addr(s):
    return int(s, 16) if s.lower().startswith("0x") else int(s, 0)


def attach(name):
    pid = find_pid(name)
    if pid is None:
        print(f"{name} is not running.")
        sys.exit(1)
    return Process(pid)


# ------------------------------------------------------------------ fields

def cmd_fields(args):
    proc = attach(args.process)
    specs = []
    for item in args.addr:
        if "=" in item:
            label, a = item.split("=", 1)
        else:
            label, a = item, item
        specs.append((label, parse_addr(a)))
    readers = {"u8": proc.u8, "u16": proc.u16, "u32": proc.u32,
               "i32": proc.i32, "f32": proc.f32}
    read = readers[args.dtype]
    print(f"watching {len(specs)} fields as {args.dtype}; ctrl-c to stop")
    last = {}
    try:
        while True:
            line = []
            changed = False
            for label, a in specs:
                v = read(a)
                if last.get(label) != v:
                    changed = True
                last[label] = v
                line.append(f"{label}={v}")
            if changed:
                print(f"{time.strftime('%H:%M:%S')}  " + "  ".join(line))
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        proc.close()


# ------------------------------------------------------------------ struct

def cmd_struct(args):
    proc = attach(args.process)
    base = parse_addr(args.addr) - args.before
    size = args.before + args.after
    activity = np.zeros(size, dtype=np.float64)
    prev = None
    print(f"dumping 0x{base:012X}..0x{base + size:012X}; ctrl-c to stop")
    try:
        frame = 0
        while True:
            cur = np.frombuffer(proc.read_tolerant(base, size), dtype=np.uint8)
            if prev is not None:
                activity = activity * 0.97 + (cur != prev) * 1.0
            prev = cur
            frame += 1
            if frame % 30 == 0:
                print("\n" * 2)
                print(f"base 0x{base:012X}   (+ = byte is changing)")
                for row in range(0, size, 16):
                    chunk = cur[row:row + 16]
                    act = activity[row:row + 16]
                    hexs = " ".join(
                        (f"\x1b[93m{b:02X}\x1b[0m" if a > 0.3 else f"{b:02X}")
                        for b, a in zip(chunk, act))
                    off = row - args.before
                    sign = "+" if off >= 0 else "-"
                    print(f"  0x{base + row:012X} [{sign}0x{abs(off):03X}]  {hexs}")
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        proc.close()


# ---------------------------------------------------------------- classify

def analyse(sessions, labels, base, before, min_frames=30):
    """Find offsets whose values are exclusive to a single label."""
    views = []
    for width, dt in ((1, np.uint8), (2, np.uint16), (4, np.uint32)):
        per_label = {}
        ok = True
        for lab in labels:
            arr = sessions.get(lab)
            if arr is None or arr.shape[0] < min_frames:
                ok = False
                break
            usable = (arr.shape[1] // width) * width
            per_label[lab] = arr[:, :usable].view(dt)
        if ok:
            views.append((width, per_label))

    findings = []
    for width, per_label in views:
        ncols = min(v.shape[1] for v in per_label.values())
        uniques = {lab: [np.unique(per_label[lab][:, c])
                         for c in range(ncols)] for lab in labels}
        for c in range(ncols):
            sets = {lab: set(uniques[lab][c].tolist()) for lab in labels}
            exclusive = {}
            fail = False
            for lab in labels:
                others = set()
                for other in labels:
                    if other != lab:
                        others |= sets[other]
                own = sets[lab] - others
                if not own or len(own) > 4:
                    fail = True
                    break
                exclusive[lab] = sorted(own)
            if fail:
                continue
            spread = sum(len(sets[lab]) for lab in labels) / len(labels)
            score = 1.0 / (1.0 + sum(len(v) for v in exclusive.values())
                           - len(labels)) / (1.0 + spread / 16.0)
            findings.append({
                "address": base + c * width,
                "offset": c * width - before,
                "width": width,
                "score": round(float(score), 4),
                "values": {lab: [int(x) for x in exclusive[lab]]
                           for lab in labels},
            })
    findings.sort(key=lambda f: (-f["score"], f["width"]))
    return findings


def cmd_classify(args):
    proc = attach(args.process)
    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    if len(labels) > 10:
        print("at most 10 labels (F1..F10)")
        sys.exit(1)
    base = parse_addr(args.addr) - args.before
    size = args.before + args.after

    print(__doc__)
    print(f"window 0x{base:012X}..0x{base + size:012X} ({size} bytes)")
    for i, lab in enumerate(labels):
        print(f"  F{i + 1}  toggle recording for label {lab!r}")
    print("  F9  analyse    F7  save attack_table.json    F12 quit\n")

    sessions = {}
    buffers = {lab: [] for lab in labels}
    active = None
    keys = Keys()
    findings = []

    def stop_active():
        nonlocal active
        if active is None:
            return
        sessions[active] = (np.stack(buffers[active])
                            if buffers[active] else None)
        n = 0 if sessions[active] is None else sessions[active].shape[0]
        print(f"  stopped {active!r}: {n} frames")
        beep(700, 70)
        active = None

    next_tick = time.perf_counter()
    try:
        while True:
            for k in keys.pressed():
                if k == "F12":
                    raise KeyboardInterrupt
                if k == "F9":
                    stop_active()
                    findings = analyse(sessions, labels, base, args.before)
                    print(f"\n=== {len(findings)} candidate fields ===")
                    print(f"  {'address':<12}{'offset':>9}{'w':>3}"
                          f"{'score':>8}   exclusive values per label")
                    for f in findings[:20]:
                        vals = "  ".join(
                            f"{lab}={','.join(map(str, v))}"
                            for lab, v in f["values"].items())
                        off = f["offset"]
                        sign = "+" if off >= 0 else "-"
                        print(f"  0x{f['address']:012X}{sign}0x{abs(off):<6X}"
                              f"{f['width']:>3}{f['score']:>8.3f}   {vals}")
                    if not findings:
                        print("  nothing separates the labels. Record longer "
                              "sessions, or widen --after.")
                    print()
                    beep(1400, 120)
                elif k == "F7":
                    blob = {"base": base, "before": args.before,
                            "size": size, "labels": labels,
                            "findings": findings[:200]}
                    with open("attack_table.json", "w") as fh:
                        json.dump(blob, fh, indent=2)
                    print("  wrote attack_table.json")
                    beep(1000, 120)
                elif k in VKF and k != "F11":
                    i = int(k[1:]) - 1
                    if 0 <= i < len(labels):
                        lab = labels[i]
                        if active == lab:
                            stop_active()
                        else:
                            stop_active()
                            active = lab
                            buffers[lab] = []
                            print(f"  recording {lab!r}... perform only that "
                                  f"attack type, F{i + 1} again to stop")
                            beep(1600, 70)

            if active is not None:
                buffers[active].append(
                    np.frombuffer(proc.read_tolerant(base, size),
                                  dtype=np.uint8))

            next_tick += 1 / 60
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        proc.close()
        print("detached")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--process", default="DOA6LR.exe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fields")
    f.add_argument("addr", nargs="+", help="addr or label=addr")
    f.add_argument("--dtype", default="u32",
                   choices=["u8", "u16", "u32", "i32", "f32"])
    f.set_defaults(func=cmd_fields)

    s = sub.add_parser("struct")
    s.add_argument("addr")
    s.add_argument("--before", type=lambda x: int(x, 0), default=0x100)
    s.add_argument("--after", type=lambda x: int(x, 0), default=0x200)
    s.set_defaults(func=cmd_struct)

    c = sub.add_parser("classify")
    c.add_argument("addr")
    c.add_argument("--before", type=lambda x: int(x, 0), default=0x100)
    c.add_argument("--after", type=lambda x: int(x, 0), default=0x400)
    c.add_argument("--labels", default="high,midp,midk,low")
    c.set_defaults(func=cmd_classify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
