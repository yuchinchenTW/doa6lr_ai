"""
comboreplay.py - Combo Challenge: watch the game's own demonstration, then
play the same inputs back.

In Combo Challenge the game demonstrates the required combo on request. The
demo drives OUR character, so every input it makes shows up in memory as a
CommandCode change (P = 1000, 6P = 1060, K = 1100, 2K = 1120: button base
plus numpad digit x 10) together with the move id it produced and the frame
of the previous move at which the next command registered. That is the
whole recipe: which inputs, and when.

    python comboreplay.py                 # facing right (P1 on the left)
    python comboreplay.py --facing left

Flow, all from inside the game:
    1. start the demonstration (the game's own button for it)
       -> the tool records automatically and prints the decoded sequence
    2. F8   replay the last recording (as often as you like)
       F5   forget it and wait for a new demonstration
       F7   save the recording to demo.json
       F10  quit

Decoding: known bases P 1000 / K 1100; PK, HK and S are guesses (1200 /
1300 / 1400) until a demo shows them - unknown codes are printed raw as
cmdNNNN and replayed as nothing, so compare the printout with the input
list on screen and report what the raw ones were.
"""
import argparse
import ctypes
import json
import sys
import time

from fields import load_layout, locate_all
from holdbot import Side
from memlib import Process, find_pid
from pad import KeyboardInjector, dirs_to_names

u32 = ctypes.WinDLL("user32", use_last_error=True)
VK = {"F5": 0x74, "F7": 0x76, "F8": 0x77, "F10": 0x79}

# P 1000 / K 1100 seen in matches; 5700 is P+K (a Combo Challenge demo of
# 8P+K read cmd 5780 and produced move 8428 - the 84xx ids are the P+K
# family seen from pokes). H+K and S bases are still unknown.
BUTTON_BASE = {1000: "P", 1100: "K", 5700: "PK"}
BUTTON_KEY = {"P": "punch", "K": "kick", "PK": "pk", "HK": "hk", "S": "special",
              "T": "throw", "H": "free"}
NUMPAD = {1: (-1, -1), 2: (0, -1), 3: (1, -1), 4: (-1, 0), 5: (0, 0),
          6: (1, 0), 7: (-1, 1), 8: (0, 1), 9: (1, 1)}
IDLE_MOVES = (0, 1, 2, 3, 4)


def decode(cmd):
    """CommandCode -> (numpad digit or 0, button token) or None."""
    if cmd == 363:
        return 0, "T"
    if 364 <= cmd <= 369:
        return {364: 6, 365: 4, 366: 2, 367: 1, 368: 8, 369: 3}[cmd], "T"
    if cmd == 168:
        return 0, "H"
    for base, btn in BUTTON_BASE.items():
        if base <= cmd < base + 100 and (cmd - base) % 10 == 0:
            return (cmd - base) // 10, btn
    return None


def token(cmd):
    d = decode(cmd)
    if d is None:
        return f"cmd{cmd}"
    digit, btn = d
    return f"{digit if digit else ''}{btn}"


class Hotkeys:
    def __init__(self):
        self.down = set()

    def pressed(self):
        hits = []
        for name, vk in VK.items():
            is_down = bool(u32.GetAsyncKeyState(vk) & 0x8000)
            if is_down and name not in self.down:
                hits.append(name)
            (self.down.add if is_down else self.down.discard)(name)
        return hits


def record(sides, hot, rebind):
    """Wait for the demonstration to start, log every command / move change
    until both characters have been idle for 1.2 s. Returns the event list.

    The demo may run on either object (or on freshly created ones - the
    anchors are re-resolved every half second while waiting), so both sides
    are watched and the one that attacks first is the performer."""
    print("waiting for the demonstration (start it in the game)...")
    events = []
    started = None
    last_cmd = last_mv = None
    idle_since = None
    me = foe = None
    seen = {}
    t_bind = 0.0
    while True:
        for k in hot.pressed():
            if k == "F10":
                return None, None
        now = time.perf_counter()
        if started is None:
            if now - t_bind > 0.5:
                sides = rebind() or sides
                t_bind = now
            for name, sd in sides.items():
                sd.refresh()
                st = (sd.get("CurrentMove"), sd.get("MoveKind"), sd.get("CommandCode"))
                if seen.get(name) != st:
                    seen[name] = st
                    print(f"    [{name}] move {st[0]} kind {st[1]} cmd {st[2]}")
                if st[0] not in IDLE_MOVES and st[1] in (3, 16, 5):
                    me = sd
                    foe = [o for n, o in sides.items() if n != name][0]
                    started = now
                    last_cmd, last_mv = st[2], None
                    print(f"  demo started on {name}")
                    if st[2]:
                        # the first command is already in place when we
                        # notice the demo: record it (a one-move stage has
                        # nothing else, and the first recording had 0 inputs)
                        events.append({"t": 0.0, "cmd": int(st[2]), "tok": token(st[2]),
                                       "prev_mv": 0, "prev_fr": 0})
                        print(f"   0.000s  input {token(st[2]):<6} (cmd {st[2]})  first")
                    break
            if started is None:
                time.sleep(0.002)
                continue
        me.refresh(); foe.refresh()
        cmd, mv, fr = me.get("CommandCode"), me.get("CurrentMove"), me.get("CurrentMoveFrame")
        if cmd != last_cmd and cmd:
            events.append({"t": round(now - started, 3), "cmd": int(cmd), "tok": token(cmd),
                           "prev_mv": int(last_mv) if last_mv is not None else int(mv),
                           "prev_fr": int(fr)})
            print(f"  {now - started:6.3f}s  input {token(cmd):<6} (cmd {cmd})  "
                  f"during move {last_mv if last_mv is not None else mv} frame {fr}")
        if mv != last_mv:
            events.append({"t": round(now - started, 3), "mv": int(mv), "kind": int(me.get("MoveKind"))})
            print(f"  {now - started:6.3f}s  move {mv} (kind {me.get('MoveKind')})")
            last_mv = mv
        last_cmd = cmd
        both_idle = (mv in IDLE_MOVES and me.get("MoveKind") == 0
                     and foe.get("MoveKind") == 0 and foe.get("CurrentMove") in IDLE_MOVES + (127, 131, 135))
        if both_idle and now - started > 1.0:
            idle_since = idle_since or now
            if now - idle_since > 1.2:
                break
        else:
            idle_since = None
        time.sleep(0.001)
    return events, me


def plan(events):
    """Turn the raw event list into replay steps: (token, move produced,
    press when the previous produced move reaches this frame)."""
    steps = []
    for i, e in enumerate(events):
        if "cmd" not in e:
            continue
        produced = None
        for f in events[i + 1:]:
            if "mv" in f and f["mv"] not in IDLE_MOVES:
                produced = f["mv"]
                break
            if "cmd" in f:
                break
        steps.append({"tok": e["tok"], "cmd": e["cmd"], "prev_mv": e["prev_mv"],
                      "prev_fr": e["prev_fr"], "mv": produced})
    return steps


def press(inj, tok_cmd, facing_right, hold=0.045):
    d = decode(tok_cmd)
    if d is None:
        return False
    digit, btn = d
    dx, dy = NUMPAD.get(digit, (0, 0)) if digit else (0, 0)
    if not facing_right:
        dx = -dx
    key = BUTTON_KEY[btn]
    horiz = dirs_to_names(dx, 0)
    vert = dirs_to_names(0, dy)
    # The recipe the diagonal holds (7H / 1H) land with: the horizontal a
    # frame ahead, then the VERTICAL AND THE BUTTON IN ONE SendInput call.
    # A vertical sent on its own a frame early is read as a sidestep and
    # the button then comes out neutral (8P+K replayed as P+K, move 8119).
    if horiz:
        inj.down(horiz)
        time.sleep(0.017)
    inj.down(vert + [key])
    time.sleep(hold)
    inj.up([key])
    inj.up(vert + horiz)
    return True


def replay(me, steps, inj, facing_right, lag_frames=2):
    print(f"replaying {len(steps)} input(s): " + " ".join(s["tok"] for s in steps))
    me.refresh()
    results = []
    for i, s in enumerate(steps):
        if i > 0:
            # wait for the previous produced move, then for its frame
            want_mv, want_fr = steps[i - 1]["mv"], max(1, s["prev_fr"] - lag_frames)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 1.0:
                me.refresh()
                mv, fr = me.get("CurrentMove"), me.get("CurrentMoveFrame")
                if (want_mv is None or mv == want_mv) and fr >= want_fr and mv not in IDLE_MOVES:
                    break
                if mv in IDLE_MOVES and time.perf_counter() - t0 > 0.25:
                    break               # the string dropped: press anyway
                time.sleep(0.001)
        ok = press(inj, s["cmd"], facing_right)
        # what came out
        got = None
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 0.35:
            me.refresh()
            mv = me.get("CurrentMove")
            if mv not in IDLE_MOVES and mv != (steps[i - 1]["mv"] if i else None):
                got = mv
                break
            time.sleep(0.001)
        results.append((s["tok"], s["mv"], got))
        print(f"  {i + 1:>2}. {s['tok']:<6} wanted move {s['mv']}  got {got}"
              + ("" if ok else "  (unknown code, nothing pressed)")
              + ("  OK" if got == s["mv"] else ""))
    hits = sum(1 for _, w, g in results if w == g)
    print(f"  {hits}/{len(results)} moves matched the demonstration")


def calibrate(sides, inj, facing_right, hot):
    """Press every button with every direction our keys can make, read the
    CommandCode and move id the game gives each one, and print the table.
    Answers two questions at once: what each cmd number means, and which
    directions actually register through SendInput."""
    me = sides["P1"]
    order = [(0, b) for b in ("P", "K", "PK", "HK", "S", "T")]
    for d in (6, 4, 2, 8, 3, 9, 1, 7):
        order += [(d, b) for b in ("P", "K", "PK", "HK", "S")]
    # "hold the direction" commands (the screen said hold left/right + P+K
    # for the move the demo read as cmd 5780): direction held 0.35 s first
    for d in (6, 4):
        order += [(f"{d}h", b) for b in ("P", "K", "PK", "HK", "S")]
    table = {}
    print("calibrating: stand idle in the game and do not touch the keys "
          f"({len(order)} inputs, ~1 s each). F10 aborts.")
    for digit, btn in order:
        if "F10" in hot.pressed():
            break
        # wait until we are idle again
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 3.0:
            me.refresh()
            if me.get("CurrentMove") in IDLE_MOVES and me.get("MoveKind") == 0:
                break
            time.sleep(0.005)
        time.sleep(0.25)
        me.refresh()
        cmd0 = me.get("CommandCode")
        held = isinstance(digit, str) and digit.endswith("h")
        dnum = int(str(digit).rstrip("h")) if digit else 0
        dx, dy = NUMPAD.get(dnum, (0, 0)) if dnum else (0, 0)
        if not facing_right:
            dx = -dx
        horiz, vert = dirs_to_names(dx, 0), dirs_to_names(0, dy)
        key = BUTTON_KEY[btn]
        if horiz:
            inj.down(horiz); time.sleep(0.35 if held else 0.017)
        inj.down(vert + [key]); time.sleep(0.045)
        inj.up([key]); inj.up(vert + horiz)
        got_cmd = got_mv = None
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 0.4:
            me.refresh()
            c, m = me.get("CommandCode"), me.get("CurrentMove")
            if got_cmd is None and c and c != cmd0:
                got_cmd = int(c)
            if got_mv is None and m not in IDLE_MOVES:
                got_mv = int(m)
            if got_cmd is not None and got_mv is not None:
                break
            time.sleep(0.002)
        tok = f"{digit if digit else ''}{btn}"
        table[tok] = {"cmd": got_cmd, "move": got_mv}
        print(f"  {tok:<5} -> cmd {got_cmd}  move {got_mv}")
        time.sleep(0.6)
    with open("commands.json", "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1)
    print("saved commands.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true",
                    help="press every input once and print the cmd / move it produces")
    ap.add_argument("--process", default=None)
    ap.add_argument("--me", default="P1", choices=["P1", "P2"])
    ap.add_argument("--facing", default="right", choices=["right", "left"],
                    help="which way our character faces at the start of the combo")
    ap.add_argument("--lag-frames", type=int, default=2,
                    help="press this many frames earlier than the demo did (input lag)")
    args = ap.parse_args()

    layout = load_layout()
    pid = find_pid(args.process or layout["process"])
    if pid is None:
        print("game not running"); sys.exit(1)
    proc = Process(pid)
    anchors = locate_all(proc, layout)
    if any(v is None for v in anchors.values()):
        print(f"anchors did not resolve: {anchors}"); sys.exit(2)
    def rebind():
        a = locate_all(proc, layout)
        if any(v is None for v in a.values()):
            return None
        return {"P1": Side(proc, layout, a, "P1"), "P2": Side(proc, layout, a, "P2")}

    sides = rebind()
    inj = KeyboardInjector()
    hot = Hotkeys()
    facing_right = args.facing == "right"
    if args.calibrate:
        calibrate(sides, inj, facing_right, hot)
        inj.release_all()
        return

    steps = None
    while True:
        events, me = record(sides, hot, rebind)
        if events is None:
            break
        steps = plan(events)
        print("\nrecorded: " + "  ".join(f"{s['tok']}->{s['mv']}@{s['prev_fr']}" for s in steps))
        print("F8 replay   F5 record again   F7 save   F10 quit")
        while True:
            keys = hot.pressed()
            if "F10" in keys:
                inj.release_all(); return
            if "F5" in keys:
                break
            if "F7" in keys:
                with open("demo.json", "w", encoding="utf-8") as fh:
                    json.dump({"events": events, "steps": steps}, fh, indent=1)
                print("  saved demo.json")
            if "F8" in keys:
                replay(me, steps, inj, facing_right, args.lag_frames)
                print("F8 replay   F5 record again   F7 save   F10 quit")
            time.sleep(0.01)


if __name__ == "__main__":
    main()
