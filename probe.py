"""
probe.py - record the character objects while scripted moves happen, then
work out which offsets are which fields.

    python probe.py record            focus the game, perform moves, save probe.npz
    python probe.py analyse           read probe.npz, print candidate fields

The objects come from layout.json's anchor chain or from --a/--b on the
command line (absolute addresses for this session). Each action records
both objects at ~120 Hz for about a second: press, then read, read, read.
Reading 1 MB from another process takes about a millisecond, so the whole
object is captured every frame and nothing has to be guessed in advance.

Actions performed (default keyboard: K punch, L kick, arrows):
    idle        nothing
    punch       K           high punch
    kick        L           high/mid kick
    lowkick     2+L         low kick
    midpunch    6+P         forward punch (character dependent)
    crouch      hold 2
    walk        hold 6 then 4

analyse looks for, per offset and width:
  * move id   - constant for the whole of one action, different between
                actions, and not the same as idle
  * startup   - constant during a move, small (4..60), differs between moves
  * counters  - increase by one per frame (the known frame counter is one)
  * strike    - a byte whose value during punch, kick and lowkick differ from
                each other (the StrikeType candidates)
It prints tables; nothing is written to layout.json automatically.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time

import numpy as np

from memlib import Process, find_pid
from pad import (KeyboardInjector, disable_high_res_timer,
                 enable_high_res_timer, hold)

u32 = ctypes.WinDLL("user32", use_last_error=True)
args_me_right = [False]      # set from --me-right before recording
args_pretap = [0.020]        # set from --pretap

PRESETS = {
    # name,     keys held (direction), tap button, seconds to record
    "offense": [
        ("idle",     [],        None,    0.8),
        ("punch",    [],        "punch", 1.2),
        ("kick",     [],        "kick",  1.2),
        ("lowkick",  ["down"],  "kick",  1.2),
        ("fwdpunch", ["right"], "punch", 1.2),
        ("crouch",   ["down"],  None,    0.8),
        ("walk",     ["right"], None,    0.8),
    ],
    # what the bot itself will be doing: throws, the four holds, a guard.
    # Directions are screen-space here; run it with us on the LEFT side
    # (facing right), so back = left.
    "defense": [
        ("idle",     [],              None,    0.8),
        ("throw",    [],              "throw", 1.5),
        ("hold7",    ["left", "up"],  "free",  1.2),
        ("hold4",    ["left"],        "free",  1.2),
        ("hold6",    ["right"],       "free",  1.2),
        ("hold1",    ["left", "down"], "free", 1.2),
        ("guard",    [],              "free",  0.8),
        ("sidestep", ["up"],          None,    1.0),
    ],
    # The four holds exactly as pad.hold() performs them for the bot
    # (pretap the opposite direction, then direction + Free in one go). The
    # "defense" preset pressed the direction 50 ms early and 7/1 came out as
    # movement + guard instead of a hold, so this is the test that matters.
    "holds": [
        ("idle",      [], None,        0.8),
        ("hold_high", [], "HOLD:high", 1.2),
        ("hold_midp", [], "HOLD:midp", 1.2),
        ("hold_midk", [], "HOLD:midk", 1.2),
        ("hold_low",  [], "HOLD:low",  1.2),
    ],
}
ACTIONS = PRESETS["offense"]


def game_hwnd(pid):
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                out.append(hwnd)
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out[0] if out else None


def focus(pid, inj):
    h = game_hwnd(pid)
    if not h:
        return False
    inj.tap_key("lalt", 0.02)
    u32.ShowWindow(h, 9)
    u32.SetForegroundWindow(h)
    time.sleep(0.25)
    return u32.GetForegroundWindow() == h


def record(proc, inj, objs, size, rate=120, actions=None):
    """objs: {label: base}. Returns {action: {label: (frames, size) u8}}."""
    out = {}
    for name, dirs, button, seconds in (actions or ACTIONS):
        time.sleep(1.2)                       # back to neutral
        frames = {l: [] for l in objs}
        stamps = []
        if dirs:
            inj.down(dirs)
            time.sleep(0.05)
        t0 = time.perf_counter()
        if button and button.startswith("HOLD:"):
            # facing_right=True: run this with us on the left side
            hold(inj, button[5:], facing_right=not args_me_right[0],
                 pretap=args_pretap[0])
            button = None
        elif button:
            inj.down([button])
        n = 0
        while time.perf_counter() - t0 < seconds:
            for l, base in objs.items():
                frames[l].append(np.frombuffer(
                    proc.read_tolerant(base, size), dtype=np.uint8))
            stamps.append(time.perf_counter() - t0)
            n += 1
            if button and n == 4:
                inj.up([button])
            time.sleep(1 / rate)
        if button:
            inj.up([button])
        if dirs:
            inj.up(dirs)
        out[name] = {l: np.stack(frames[l]) for l in objs}
        out[name]["t"] = np.array(stamps)
        print(f"  {name:<9} {len(stamps)} frames")
    return out


def cmd_record(args):
    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running.")
        sys.exit(1)
    proc = Process(pid)
    if args.a and args.b:
        objs = {"A": int(args.a, 16), "B": int(args.b, 16)}
    else:
        from fields import load_layout, locate_all
        lay = load_layout()
        an = locate_all(proc, lay)
        objs = {"A": an["state"] + lay["players"]["P1"]["state"],
                "B": an.get("state:P2", an["state"]) + lay["players"]["P2"]["state"]}
    print(f"  A=0x{objs['A']:012X}  B=0x{objs['B']:012X}")
    inj = KeyboardInjector()
    enable_high_res_timer()
    try:
        args_me_right[0] = args.me_right
        args_pretap[0] = args.pretap
        print(f"  game window {'focused' if focus(pid, inj) else 'NOT focused'}")
        print(f"  starting in {args.countdown:.0f}s; hands off")
        time.sleep(args.countdown)
        data = record(proc, inj, objs, args.size,
                      actions=PRESETS[args.preset])
    finally:
        inj.close()
        disable_high_res_timer()
        proc.close()
    blob = {"size": np.array(args.size), "A": np.array(objs["A"]),
            "B": np.array(objs["B"])}
    for act, d in data.items():
        for l, arr in d.items():
            blob[f"{act}__{l}"] = arr
    np.savez_compressed(args.out, **blob)
    print(f"  wrote {args.out}")


# ----------------------------------------------------------------- analyse

def views(arr, width):
    usable = (arr.shape[1] // width) * width
    dt = {1: np.uint8, 2: np.uint16, 4: np.uint32}[width]
    return arr[:, :usable].view(dt)


def cmd_analyse(args):
    z = np.load(args.file)
    size = int(z["size"])
    acts = sorted({k.split("__")[0] for k in z.files if "__" in k})
    label = args.obj
    data = {a: z[f"{a}__{label}"] for a in acts}
    stamps = {a: z[f"{a}__t"] for a in acts}
    print(f"object {label} base 0x{int(z[label]):012X}, {size} bytes, "
          f"actions {acts}")
    moves = [a for a in ("punch", "kick", "lowkick", "fwdpunch") if a in data]

    for width in (1, 2, 4):
        V = {a: views(data[a], width) for a in acts}
        ncol = min(v.shape[1] for v in V.values())
        idle = V["idle"][:, :ncol]
        idle_const = (idle == idle[0]).all(axis=0)
        idle_v = idle[0].astype(np.int64)

        # A "during the move" window: frames 0.10..0.40 s after the press.
        # Startup of most strikes is 10-25 frames, so this is inside the
        # move for every one of them and past any input-buffer frames.
        const_all = np.ones(ncol, dtype=bool)
        vals = []
        for a in moves:
            t = stamps[a]
            d = V[a][(t > 0.10) & (t < 0.40), :ncol]
            const_all &= (d == d[0]).all(axis=0)
            vals.append(d[0].astype(np.int64))
        vals = np.stack(vals)                       # (nmoves, ncol)
        srt = np.sort(vals, axis=0)
        distinct = 1 + (srt[1:] != srt[:-1]).sum(axis=0)
        differs_idle = ~(idle_const & (vals == idle_v).all(axis=0))
        keep = const_all & (distinct >= 2) & differs_idle
        if width == 1:
            keep &= (vals <= 250).all(axis=0)
        cols = np.flatnonzero(keep)
        order = np.lexsort((cols, -distinct[cols]))
        print(f"\n=== width {width}: {len(cols)} offsets constant during each "
              f"move and differing between moves ===")
        print(f"  {'offset':<10}{'idle':>8}  " +
              "".join(f"{a:>10}" for a in moves))
        for c in cols[order][:args.top]:
            iv = str(int(idle_v[c])) if idle_const[c] else "varies"
            print(f"  +0x{c * width:<7X}{iv:>8}  " +
                  "".join(f"{int(v):>10}" for v in vals[:, c]))

    # per-frame counters in the punch recording (should include the known one)
    V = views(data["punch"], 2)
    t = stamps["punch"]
    m = (t > 0.15) & (t < 0.60)
    seg = V[m].astype(np.int64)
    d = np.diff(seg, axis=0)
    dt = np.diff(t[m])
    expect = np.round(dt * 60)
    good = (np.abs(d - expect[:, None]) <= 1).all(axis=0) & (seg[-1] > seg[0])
    print(f"\n=== u16 offsets advancing one per frame during the punch: "
          f"{int(good.sum())} ===")
    for c in np.flatnonzero(good)[:20]:
        print(f"  +0x{c * 2:X}  {seg[0, c]} -> {seg[-1, c]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--process", default="DOA6LR.exe")
    r.add_argument("--a", help="hex base of our object (default: layout.json)")
    r.add_argument("--b", help="hex base of the opponent's")
    r.add_argument("--preset", default="offense", choices=sorted(PRESETS))
    r.add_argument("--pretap", type=float, default=0.020,
                   help="opposite-direction tap before a HOLD: action, "
                        "seconds; 0 disables (the bot's --pretap)")
    r.add_argument("--me-right", action="store_true",
                   help="we stand on the RIGHT (facing left); mirrors holds")
    r.add_argument("--size", type=lambda x: int(x, 0), default=0x10DFA0)
    r.add_argument("--countdown", type=float, default=3.0)
    r.add_argument("--out", default="probe.npz")
    r.set_defaults(func=cmd_record)
    a = sub.add_parser("analyse")
    a.add_argument("--file", default="probe.npz")
    a.add_argument("--obj", default="A", choices=["A", "B"])
    a.add_argument("--top", type=int, default=40)
    a.set_defaults(func=cmd_analyse)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
