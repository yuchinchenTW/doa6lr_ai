"""
inputtest.py - ask the game what each key does, instead of guessing bindings.

A hold that never comes out looks exactly like a hold with bad timing: the
character moves, nothing counters, and no amount of window tuning helps. This
separates the two by pressing keys and reading what the character actually did.

    python inputtest.py --find     press every key, report the action it caused
    python inputtest.py --holds    run the four hold combos, did a hold come out
    python inputtest.py --binds    print a pad.py binding block from --find
    python inputtest.py --record   log your own moves while you play

Run it with the game focused, in a training match, character standing idle and
NOT in a corner (direction keys need room to move).

Needs a working layout.json (CurrentMove, StrikeType, MoveType, XAxis for the
side you play); until then autoscan.py is the tool.
"""

import argparse
import sys
import time

from fields import STRIKE_TYPE, locate, read_field
from memlib import Process, find_pid
from pad import (KeyboardInjector, disable_high_res_timer,
                 enable_high_res_timer, hold)

# Keys worth probing. Deliberately excludes esc/enter/tab, which open menus.
CANDIDATES = (
    ["up", "down", "left", "right"]
    + list("abcdefghijklmnopqrstuvwxyz")
    + [str(d) for d in range(10)]
    + [f"num{d}" for d in range(10)]
    + ["space", "lshift", "rshift", "lctrl", "lalt", "comma", "period",
       "slash", "semicolon", "quote", "lbracket", "rbracket"]
)


def sample(proc, anchor, me):
    return (read_field(proc, anchor, f"{me}_CurrentMove"),
            read_field(proc, anchor, f"{me}_StrikeType"),
            read_field(proc, anchor, f"{me}_MoveType"),
            read_field(proc, anchor, f"{me}_XAxis"))


def wait_idle(proc, anchor, me, timeout=3.0, stable=0.25):
    """Block until our character's move id stops changing."""
    end = time.perf_counter() + timeout
    last, since = None, time.perf_counter()
    while time.perf_counter() < end:
        cur = read_field(proc, anchor, f"{me}_CurrentMove")
        if cur != last:
            last, since = cur, time.perf_counter()
        elif time.perf_counter() - since >= stable:
            return last
        time.sleep(0.01)
    return last


def classify(base, states, move_eps=0.02):
    """Turn the observed states into a guess at what the key is bound to.

    StrikeType must never be read on its own here: it latches, keeping the last
    attack's value while the character stands idle, so every key that changed
    the animation at all would otherwise look like a punch. MoveType is the
    field that says what is happening *now* (0 idle/movement, 2/3 strike,
    8/9 stun), and the X coordinate separates walking from an in-place action.
    """
    if not states:
        return "nothing", ""
    moves = sorted({m for m, _, _, _ in states})
    types = sorted({t for _, _, t, _ in states if t is not None})

    xs = [x for _, _, _, x in states if x is not None]
    x0 = base[3]
    drift = max(abs(x - x0) for x in xs) if xs and x0 is not None else 0.0

    # Only trust StrikeType on samples where the game says we are striking.
    striking = [s for _, s, t, _ in states if t in (2, 3) and s in range(6)]
    if striking:
        s = striking[0]
        return ("punch" if s in (0, 2, 4) else "kick"), \
               f"{STRIKE_TYPE[s]}, moves {moves}"
    if types and set(types) & {2, 3}:
        return "attack?", f"MoveType {types}, moves {moves}"
    if types and set(types) & {8, 9}:
        return "stun", f"MoveType {types}"
    if drift > move_eps:
        return "movement", f"x moved {drift:.3f}, moves {moves}"
    return "in-place", f"MoveType {types}, moves {moves}, x {drift:.3f}"


def probe_key(proc, anchor, me, inj, key, watch=0.45):
    wait_idle(proc, anchor, me)
    base = sample(proc, anchor, me)
    inj.tap_key(key)
    seen, end = [], time.perf_counter() + watch
    while time.perf_counter() < end:
        s = sample(proc, anchor, me)
        if s[0] != base[0] and (not seen or s != seen[-1]):
            seen.append(s)
        time.sleep(0.008)
    return base, seen


def cmd_find(proc, anchor, me, args):
    inj = KeyboardInjector()
    print(f"probing {len(CANDIDATES)} keys on {me}; keep the game focused "
          f"and the character idle.\nStarting in 4s...\n")
    time.sleep(4)
    results = {}
    print(f"  {'key':<10}{'action':<12}detail")
    for key in CANDIDATES:
        try:
            base, seen = probe_key(proc, anchor, me, inj, key)
        except KeyboardInterrupt:
            break
        action, detail = classify(base, seen)
        results[key] = (action, detail)
        if action != "nothing":
            print(f"  {key:<10}{action:<12}{detail}")
    inj.close()

    print("\nsummary of keys that did something:")
    for want in ("punch", "kick", "in-place", "attack?", "movement",
                 "stun"):
        keys = [k for k, (a, _) in results.items() if a == want]
        if keys:
            print(f"  {want:<12} {', '.join(keys)}")
    unknown = [k for k, (a, _) in results.items() if a == "nothing"]
    print(f"  {'nothing':<12} {len(unknown)} keys")
    return results


def cmd_binds(results):
    def pick(action, exclude=()):
        for k, (a, _) in results.items():
            if a == action and k not in exclude:
                return k
        return None
    moves = [k for k, (a, _) in results.items() if a == "movement"]
    holds = [k for k, (a, _) in results.items() if a == "in-place"]
    print("\nsuggested pad.py KeyboardInjector binds (verify before trusting):")
    print("        self.binds = binds or {")
    for name, val in (("up", None), ("down", None),
                      ("left", None), ("right", None)):
        guess = name if name in moves else "?"
        print(f'            "{name}": "{guess}",')
    print(f'            "punch": "{pick("punch") or "?"}",')
    print(f'            "kick": "{pick("kick") or "?"}",')
    print(f'            "free": "{holds[0] if holds else "?"}",')
    print(f'            "throw": "{holds[1] if len(holds) > 1 else "?"}",')
    print("        }")
    if len(holds) > 1:
        print(f"\n  NOTE: {len(holds)} keys act in place ({', '.join(holds)}).")
        print("  Hold, throw and taunt all look alike from here. Try each as")
        print("  `free` with --holds; only the real Hold key makes all four")
        print("  directions produce four different move ids.")


def cmd_holds(proc, anchor, me, args):
    inj = KeyboardInjector()
    if args.free:
        inj.binds["free"] = args.free
    print(f"testing the four holds with free='{inj.binds['free']}'.\n"
          f"Starting in 4s; keep the game focused.\n")
    time.sleep(4)
    print(f"  {'input':<10}{'reacted':<10}{'move id':<12}detail")

    # The Free button alone should put the character into a guard stance.
    wait_idle(proc, anchor, me)
    base = sample(proc, anchor, me)
    inj.tap_key(inj.binds["free"], 0.12)
    seen = watch_for(proc, anchor, me, base, 0.5)
    action, detail = classify(base, seen)
    print(f"  {'free only':<10}{'YES' if seen else 'NO':<10}"
          f"{str(seen[0][0]) if seen else '-':<12}{action}  {detail}")
    time.sleep(0.6)

    ids = {}
    for kind in ("high", "midp", "midk", "low"):
        wait_idle(proc, anchor, me)
        base = sample(proc, anchor, me)
        # No pretap here: it walks the character a step, and that movement
        # swamps the very signal this test is looking for.
        hold(inj, kind, facing_right=True, pretap=0,
             press=args.press)
        seen = watch_for(proc, anchor, me, base, 0.5)
        action, detail = classify(base, seen)
        ids[kind] = seen[0][0] if seen else None
        print(f"  {kind:<10}{'YES' if seen else 'NO':<10}"
              f"{str(ids[kind]):<12}{action}  {detail}")
        time.sleep(0.6)
    inj.close()

    distinct = len({v for v in ids.values() if v is not None})
    print(f"\n  {distinct} distinct move ids across the four directions")
    print("  4 distinct + in-place -> this IS the Hold key; tune timing next\n"
          "  action=movement       -> the character just walked; wrong key\n"
          "  reacted=NO everywhere -> input is not reaching the game at all")


def watch_for(proc, anchor, me, base, seconds):
    seen, end = [], time.perf_counter() + seconds
    while time.perf_counter() < end:
        s = sample(proc, anchor, me)
        if s[0] != base[0] and (not seen or s != seen[-1]):
            seen.append(s)
        time.sleep(0.008)
    return seen


def cmd_record(proc, anchor, me, args):
    """Log what OUR character does, so a human can demonstrate a real hold.

    Guessing which key is bound to Hold has a dead end: several keys produce
    in-place stances and none of them is obviously the counter. Far quicker to
    let someone who can already play the game perform one, and read off the
    move id it produces - that becomes the target the injector has to hit.
    """
    print(f"logging {me}. Perform a few holds by hand (let the CPU attack you),\n"
          f"then ctrl-c. Note which key you pressed.\n")
    print(f"  {'move':<8}{'MoveType':<10}{'StrikeType':<12}{'x drift':<10}note")
    last_move, x_ref = None, None
    try:
        while True:
            move, strike, mtype, x = sample(proc, anchor, me)
            if move != last_move:
                if x_ref is None and x is not None:
                    x_ref = x
                drift = abs((x or 0) - (x_ref or 0))
                note = ""
                if mtype in (2, 3):
                    note = f"striking ({STRIKE_TYPE.get(strike, '?')})"
                elif mtype in (8, 9):
                    note = "stunned"
                elif drift < 0.02:
                    note = "in place"
                print(f"  {str(move):<8}{str(mtype):<10}{str(strike):<12}"
                      f"{drift:<10.3f}{note}")
                last_move, x_ref = move, x
            time.sleep(0.008)
    except KeyboardInterrupt:
        print("\n  Look for a run of ids that appear only when you counter an "
              "attack;\n  those are the hold animations we need to reproduce.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--process", default="DOA6LR.exe")
    ap.add_argument("--me", default="P1", choices=["P1", "P2"])
    ap.add_argument("--find", action="store_true")
    ap.add_argument("--holds", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="log our own moves while you play by hand")
    ap.add_argument("--binds", action="store_true")
    ap.add_argument("--free", help="override the Hold key for --holds")
    ap.add_argument("--press", type=float, default=0.10,
                    help="how long to hold the keys, seconds. The bot uses "
                         "0.02; diagnose with a generous value so a too-short "
                         "press is not mistaken for a wrong key")
    args = ap.parse_args()
    if not (args.find or args.holds or args.record):
        ap.error("pick --find, --holds or --record")

    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running.")
        sys.exit(1)
    proc = Process(pid)
    anchor = locate(proc)
    if anchor is None:
        print("layout.json has no working anchor yet; see README.")
        sys.exit(2)
    print(f"anchor 0x{anchor:012X}")
    enable_high_res_timer()
    try:
        if args.find:
            results = cmd_find(proc, anchor, args.me, args)
            if args.binds:
                cmd_binds(results)
        if args.holds:
            cmd_holds(proc, anchor, args.me, args)
        if args.record:
            cmd_record(proc, anchor, args.me, args)
    finally:
        disable_high_res_timer()
        proc.close()


if __name__ == "__main__":
    main()
