"""
autoscan.py - find DOA6LR's fields without a human at the keyboard.

The DOA5 hunt was hotkey-driven: someone stood in training mode pressing F2
and F3 between crouches. Here the script presses the game's own keys through
SendInput while it filters, so a hunt is one command and a couple of minutes.

    python autoscan.py frame            per-move frame counter (the key field)
    python autoscan.py position         our X coordinate
    python autoscan.py crouch           stance / posture state fields
    python autoscan.py health           the dummy's health
    python autoscan.py all              the four above, in that order

Requirements: DOA6LR.exe running in FREE TRAINING with the game window
visible, us on P1 with room to walk right, the dummy standing in front. The
script brings the window to the front itself. Default keyboard layout
(arrows, K punch, L kick, J hold, M throw); override with --binds.

Why the frame counter first. It is the one field with a signature that needs
no timing at all: it advances by exactly one per 60 Hz frame, in idle as much
as mid-move, and when a move starts it drops back to 1. Two snapshots a
second apart with per-piece timestamps keep only things that count at 60 Hz -
a few hundred slots in an 8 GB process - and one punch then separates the
per-move counters (they reset) from global frame timers (they do not). Where
that counter lives, the rest of the character struct lives too, and from
there watch.py struct / classify do what they did for DOA5.

Every hunt writes hunt_<name>.json with the surviving addresses and a cluster
summary, and prints the same.
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
                 enable_high_res_timer)
from scanner import DTYPES, Scanner, cluster, read_values

u32 = ctypes.WinDLL("user32", use_last_error=True)


# ------------------------------------------------------------------ window

def game_windows(pid):
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                out.append((hwnd, buf.value))
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def focus_game(pid, inj):
    """Bring the game to the front so SendInput reaches it.

    SetForegroundWindow is refused unless our process recently received
    input; tapping a harmless modifier first is the standard way round that.
    """
    wins = game_windows(pid)
    if not wins:
        print("  could not find the game window")
        return False
    hwnd = wins[0][0]
    inj.tap_key("lalt", 0.02)
    u32.ShowWindow(hwnd, 9)          # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    ok = u32.GetForegroundWindow() == hwnd
    print(f"  game window {'focused' if ok else 'NOT focused'}: {wins[0][1]}")
    return ok


# ------------------------------------------------------------------- hunts

class Hunt:
    def __init__(self, proc, inj, dtype, dry=False):
        self.proc = proc
        self.inj = inj
        self.dtype = dtype
        self.sc = Scanner(proc, DTYPES[dtype], dtype)
        self.dry = dry

    # -- input helpers ----------------------------------------------------

    def down(self, names):
        if self.dry:
            print(f"    (dry) press {names}")
            return
        self.inj.down(names)

    def up(self, names):
        if not self.dry:
            self.inj.up(names)

    def press(self, names, seconds):
        self.down(names)
        time.sleep(seconds)
        self.up(names)

    def tap(self, name, seconds=0.05):
        self.press([name], seconds)

    def idle(self, seconds, why=""):
        print(f"    idle {seconds:.1f}s {why}")
        time.sleep(seconds)

    def values(self):
        return read_values(self.proc, self.sc.addrs, self.sc.dt)

    # -- reporting --------------------------------------------------------

    def report(self, name, extra=None):
        sc = self.sc
        addrs = sc.addrs if sc.addrs is not None else np.zeros(0, np.uint64)
        vals = self.values() if len(addrs) else []
        groups = cluster(addrs, gap=0x2000)
        groups.sort(key=lambda g: -g[2])
        print(f"\n=== {name}: {len(addrs)} candidates ({self.dtype}) ===")
        for lo, hi, n in groups[:10]:
            print(f"  0x{lo:012X}-0x{hi:012X}  span 0x{hi - lo:<6X} {n} hits")
        show = min(len(addrs), 40)
        for a, v in list(zip(addrs[:show], vals[:show])):
            print(f"    0x{int(a):012X}  = {v}")
        blob = {"hunt": name, "dtype": self.dtype,
                "addresses": [int(a) for a in addrs[:20000]],
                "values": [float(v) for v in vals[:20000]],
                "clusters": [{"lo": lo, "hi": hi, "n": n}
                             for lo, hi, n in groups[:50]]}
        if extra:
            blob.update(extra)
        path = f"hunt_{name}.json"
        with open(path, "w") as f:
            json.dump(blob, f, indent=1)
        print(f"  wrote {path}")
        return addrs, vals

    # -- the hunts --------------------------------------------------------

    def frame_counter(self, rounds=2, rate=60.0):
        """Slots that tick at `rate` per second, then those that reset when
        we punch. rate=60 is a frame counter; 1.0 is a float seconds clock,
        which is what a newer engine may keep instead."""
        sc = self.sc
        print(f"[frame] reference snapshot (stand still), {self.dtype} "
              f"@ {rate:g}/s")
        sc.set_reference()
        self.idle(1.0, "letting counters advance")
        tol = 6 if rate >= 10 else 0.1 * rate
        sc.filter_rate(rate, tol=tol, tag="RATE")
        for _ in range(2):
            self.idle(0.7)
            sc.filter_rate(rate, tol=tol * 0.7, tag="RATE")
        ticking = sc.addrs.copy()
        print(f"  {len(ticking)} slots advance at {rate:g}/s")
        if len(ticking) == 0:
            print("  nothing advances at that rate as this type")
            return None

        # A per-move counter restarts at 1 when a move starts. Punch, then
        # sample the survivors at 240 Hz for a second: reading a few hundred
        # slots takes well under a millisecond, so this part can afford to
        # be timed, and the window is generous.
        resets = np.ones(len(ticking), dtype=bool)
        later = np.zeros(len(ticking), dtype=bool)
        for r in range(rounds):
            self.idle(1.5, "back to neutral")
            before = read_values(self.proc, ticking, sc.dt).astype(np.float64)
            t0 = time.perf_counter()
            self.tap("punch", 0.04)
            frames, stamps = [], []
            while time.perf_counter() - t0 < 1.2:
                frames.append(read_values(self.proc, ticking, sc.dt))
                stamps.append(time.perf_counter() - t0)
                time.sleep(1 / 240)
            tl = np.stack(frames).astype(np.float64)
            st = np.array(stamps)
            early = tl[st < 0.30]
            small = 15 if rate >= 10 else 0.25 * rate   # "just restarted"
            dropped = ((early.min(axis=0) < small)
                       & (early.min(axis=0) < before))
            resets &= dropped
            # a reset 100-600ms after the press, on a slot that did not
            # reset with the press, is what the dummy's counter does when
            # our punch lands and its hit reaction starts
            mid = tl[(st > 0.10) & (st < 0.60)]
            d = np.diff(mid, axis=0)
            later |= (d < -5).any(axis=0) & ~dropped
            print(f"    round {r + 1}: {int(dropped.sum())} slots reset on "
                  f"the punch, {int(later.sum())} reset later")

        addrs_reset = ticking[resets]
        addrs_later = ticking[later]
        print(f"\n  per-move counters that reset on OUR punch: "
              f"{len(addrs_reset)}")
        for a in addrs_reset[:20]:
            print(f"    0x{int(a):012X}")
        print(f"  counters that reset a moment later (P2 hit reaction, or a "
              f"different animation layer): {len(addrs_later)}")
        for a in addrs_later[:20]:
            print(f"    0x{int(a):012X}")
        sc.addrs = addrs_reset if len(addrs_reset) else ticking
        sc.refresh_last()
        return self.report("frame", {
            "ticking_60hz": [int(a) for a in ticking[:5000]],
            "reset_on_punch": [int(a) for a in addrs_reset],
            "reset_later": [int(a) for a in addrs_later],
        })

    def position(self, rounds=3, walk=0.6):
        """Walk right / stand still; keep what moves only while we walk,
        then demand it rose while walking right and fell walking left."""
        sc = self.sc
        print("[position] reference snapshot (stand still)")
        sc.set_reference()
        print("    walking right during the compare")
        self.down(["right"])
        time.sleep(0.15)
        sc.filter(True)                       # CHANGED while walking
        self.up(["right"])
        for _ in range(rounds):
            self.idle(0.6, "stopping")
            sc.filter_activity(False)         # STILL
            print("    walking right")
            self.down(["right"])
            time.sleep(0.15)
            sc.filter_activity(True)          # MOVING
            self.up(["right"])
        # sign test: right raises it, left lowers it
        self.idle(0.6)
        a = self.values().astype(np.float64)
        self.press(["right"], walk)
        self.idle(0.4)
        b = self.values().astype(np.float64)
        self.press(["left"], walk)
        self.idle(0.4)
        c = self.values().astype(np.float64)
        keep = (b > a) & (c < b) & np.isfinite(a) & np.isfinite(b)
        if self.dtype == "f32":
            keep &= (np.abs(a) < 1000) & (np.abs(b - a) > 0.01)
        sc._push_undo()
        sc._keep(keep, self.values())
        print(f"  rose walking right and fell walking left: {len(sc.addrs)}")
        return self.report("position")

    def crouch(self, rounds=4):
        """Hold down (crouch) during CHANGED, release for SAME."""
        sc = self.sc
        print("[crouch] reference snapshot (standing)")
        sc.set_reference()
        for _ in range(rounds):
            print("    crouching")
            self.down(["down"])
            time.sleep(0.4)
            sc.filter(True)
            self.up(["down"])
            self.idle(0.6, "standing")
            sc.filter(False)
        return self.report("crouch")

    def health(self, hits=4):
        """Punch the dummy; keep what drops with every hit."""
        sc = self.sc
        print("[health] reference snapshot (nobody hurt yet)")
        sc.set_reference()
        for i in range(hits):
            self.tap("punch", 0.04)
            self.idle(0.5, "letting the hit land")
            sc.filter_delta(True)             # DROPPED since last step
            self.idle(1.0, "settling")
            if i < hits - 1:
                # training-mode health may regenerate between hits, so no
                # SAME test here; only that the next hit drops it again
                sc.refresh_last()
        return self.report("health")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hunt", choices=["frame", "position", "crouch",
                                     "health", "all"])
    ap.add_argument("--process", default="DOA6LR.exe")
    ap.add_argument("--dtype", default=None,
                    help="override the value type: frame u16, position f32, "
                         "crouch u8, health u32 by default")
    ap.add_argument("--rounds", type=int, default=None)
    ap.add_argument("--binds", default="",
                    help="key overrides, e.g. punch=k,free=j,left=a")
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--dry", action="store_true",
                    help="scan but send no keys (you act by hand instead)")
    ap.add_argument("--countdown", type=float, default=3.0)
    args = ap.parse_args()

    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running.")
        sys.exit(1)
    proc = Process(pid)
    inj = KeyboardInjector()
    for part in args.binds.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            inj.binds[k.strip()] = v.strip()
    enable_high_res_timer()

    defaults = {"frame": "u16", "position": "f32", "crouch": "u8",
                "health": "u32"}
    order = (["frame", "position", "crouch", "health"]
             if args.hunt == "all" else [args.hunt])
    try:
        if not args.no_focus and not args.dry:
            focus_game(pid, inj)
        if args.countdown:
            print(f"  starting in {args.countdown:.0f}s - leave the game "
                  f"alone, character idle, not in a corner")
            time.sleep(args.countdown)
        for name in order:
            kw = {}
            if args.rounds:
                kw["hits" if name == "health" else "rounds"] = args.rounds
            if name == "frame":
                # Nobody knows yet how DOA6LR stores the counter, so walk
                # the plausible encodings until one of them ticks: a 16- or
                # 32-bit frame count, a float frame count, a float clock.
                tries = ([(args.dtype, 60.0)] if args.dtype else
                         [("u16", 60.0), ("u32", 60.0), ("f32", 60.0),
                          ("f32", 1.0)])
                for dtype, rate in tries:
                    h = Hunt(proc, inj, dtype, dry=args.dry)
                    if h.frame_counter(rate=rate, **kw) is not None:
                        break
                    inj.release_all()
            else:
                h = Hunt(proc, inj, args.dtype or defaults[name],
                         dry=args.dry)
                getattr(h, name)(**kw)
            inj.release_all()
            print()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        inj.close()
        disable_high_res_timer()
        proc.close()


if __name__ == "__main__":
    main()
