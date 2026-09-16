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
import re
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


CMD_TABLE = {}      # cmd -> (token, button held)  from commands.json (--calibrate)


def load_commands(path="commands.json"):
    """The calibration table is the truth: P+K is 1210 neutral / 5770 with
    6 / 1230 with 4, 4K is 1540, 1P is 1014 - no formula gives those. A cmd
    exactly 10 above a known one is that input with the BUTTON HELD (the
    stage that asked for "hold P+K" read 5780 where 6P+K reads 5770)."""
    try:
        with open(path, encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        return
    for tok, v in table.items():
        c = v.get("cmd")
        if not c or tok.startswith("_"):
            continue
        cur = CMD_TABLE.get(c)
        rank = (tok.endswith("h") or "h" in tok[:-2], len(tok))
        if cur is None or rank < (cur[0].endswith("h") or "h" in cur[0][:-2], len(cur[0])):
            CMD_TABLE[c] = (tok.replace("h", ""), False)
    # codes learned by replaying (cmd -> token that produced the wanted move)
    for c_s, tok in table.get("_learned", {}).items():
        CMD_TABLE[int(c_s)] = (tok, False)
    # (the "+10 = button held" rule is gone: 5510 is the second S of the
    # Fatal Rush, 5780 is the 46P+K motion)


def split_token(tok):
    """'46PK' -> ([4, 6], 'PK'); 'P' -> ([], 'P'). Several digits are a
    motion: every direction but the last is tapped, the last one goes down
    with the button (the stage screen showed <- -> P+K, i.e. 46P+K)."""
    m = re.match(r"(\d*)([A-Z]+)", tok)
    return [int(ch) for ch in m.group(1)], m.group(2)


def decode(cmd):
    """CommandCode -> (direction digits, button token, button held) or None."""
    if cmd in CMD_TABLE:
        tok, held = CMD_TABLE[cmd]
        digits, btn = split_token(tok)
        return digits, btn, held
    if cmd == 363:
        return [], "T", False
    if 364 <= cmd <= 369:
        return [{364: 6, 365: 4, 366: 2, 367: 1, 368: 8, 369: 3}[cmd]], "T", False
    if cmd == 168:
        return [], "H", False
    for base, btn in BUTTON_BASE.items():
        if base <= cmd < base + 100 and (cmd - base) % 10 == 0:
            d = (cmd - base) // 10
            return ([d] if d else []), btn, False
    return None


# A code we have never produced ourselves: the hundreds say which button
# family it belongs to (1350..1353 were the hits of an H+K string, 1083 and
# 1085 P-family follow-ups, 5000..5002 and 5510 S-family). Candidates are
# tried in order across replays until one produces the demo's move id.
FAMILY = {10: "P", 11: "K", 12: "PK", 13: "HK", 15: "K", 55: "S", 50: "S", 57: "PK", 20: "K"}


def candidates(cmd):
    fam = FAMILY.get(cmd // 100)
    if fam is None:
        return []
    digit = (cmd % 100) // 10
    out = [fam]                                  # the plain button first: string hits
    if digit and digit in NUMPAD:
        out.append(f"{digit}{fam}")
    for other in ("P", "K", "PK", "HK", "S"):
        if other != fam:
            out.append(other)
    return out


def token(cmd):
    d = decode(cmd)
    if d is None:
        return f"cmd{cmd}"
    digits, btn, held = d
    return f"{''.join(map(str, digits))}{btn}{'(hold)' if held else ''}"


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


def press(inj, tok_cmd, facing_right, hold=0.045, tok=None):
    if tok is not None:
        digits, btn = split_token(tok)
        held = False
    else:
        d = decode(tok_cmd)
        if d is None:
            return False
        digits, btn, held = d
    if held:
        hold = 0.7                  # a charged version: keep the button down
    # a motion (46P+K): tap every direction but the last, 2 frames each
    for dg in digits[:-1]:
        ddx, ddy = NUMPAD.get(dg, (0, 0))
        if not facing_right:
            ddx = -ddx
        names = dirs_to_names(ddx, ddy)
        inj.down(names); time.sleep(0.033); inj.up(names); time.sleep(0.017)
    digit = digits[-1] if digits else 0
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


MOVE_CMDS = {9: "66", 4: "44"}      # dashes seen in a demo (moves 3 / 5)


def replay(me, steps, inj, facing_right, lag_frames=2, tries=None):
    """Play the steps back. A step the demo made from idle (prev move 0)
    waits for OUR idle; a string follow-up waits for the previous produced
    move and its frame. Unknown codes go through the family candidates,
    one per replay, and a candidate that produces the wanted move id is
    saved to commands.json so the next replay knows it."""
    tries = tries if tries is not None else {}
    print(f"replaying {len(steps)} input(s): " + " ".join(s["tok"] for s in steps))
    me.refresh()
    results, learned = [], {}
    for i, s in enumerate(steps):
        from_idle = s["prev_mv"] in IDLE_MOVES
        if i > 0:
            t0 = time.perf_counter()
            if from_idle:
                while time.perf_counter() - t0 < 2.5:      # the demo waited for idle
                    me.refresh()
                    if me.get("CurrentMove") in IDLE_MOVES and me.get("MoveKind") == 0:
                        break
                    time.sleep(0.001)
                time.sleep(0.03)
            else:
                want_mv, want_fr = steps[i - 1]["mv"], max(1, s["prev_fr"] - lag_frames)
                while time.perf_counter() - t0 < 1.0:
                    me.refresh()
                    mv, fr = me.get("CurrentMove"), me.get("CurrentMoveFrame")
                    if (want_mv is None or mv == want_mv) and fr >= want_fr and mv not in IDLE_MOVES:
                        break
                    if mv in IDLE_MOVES and time.perf_counter() - t0 > 0.25:
                        break               # the string dropped: press anyway
                    time.sleep(0.001)
        cmd = s["cmd"]
        used = None
        if decode(cmd) is not None:
            ok = press(inj, cmd, facing_right)
            used = token(cmd)
        elif cmd in MOVE_CMDS:
            used = MOVE_CMDS[cmd]
            d = 1 if (used == "66") == facing_right else -1
            names = dirs_to_names(d, 0)
            inj.down(names); time.sleep(0.033); inj.up(names); time.sleep(0.017)
            inj.down(names); time.sleep(0.05); inj.up(names)
            ok = True
        else:
            cands = candidates(cmd)
            if cands:
                n = tries.get(cmd, 0)
                used = cands[n % len(cands)]
                tries[cmd] = n + 1
                ok = press(inj, cmd, facing_right, tok=used)
            else:
                ok = False
        ids = []
        prev = steps[i - 1]["mv"] if i else None
        t1 = time.perf_counter()
        limit = 0.35 if i + 1 < len(steps) else 1.2
        while time.perf_counter() - t1 < limit:
            me.refresh()
            mv = me.get("CurrentMove")
            if mv not in IDLE_MOVES and mv != prev:
                if not ids or ids[-1] != mv:
                    ids.append(int(mv))
                if s["mv"] in ids and i + 1 < len(steps):
                    break
            elif ids:
                break
            time.sleep(0.001)
        hit = s["mv"] is not None and s["mv"] in ids
        if hit and decode(cmd) is None and used and used not in MOVE_CMDS.values():
            learned[cmd] = used
        results.append((s["tok"], s["mv"], ids))
        print(f"  {i + 1:>2}. {s['tok']:<10} wanted move {s['mv']}  got "
              f"{'>'.join(map(str, ids)) if ids else None}"
              + (f"  tried {used}" if decode(cmd) is None and used else "")
              + ("" if ok else "  (unknown code, nothing pressed)")
              + ("  OK" if hit else ""))
    hits = sum(1 for _, w, g in results if w is not None and w in g)
    print(f"  {hits}/{len(results)} moves matched the demonstration")
    if learned:
        try:
            with open("commands.json", encoding="utf-8") as fh:
                table = json.load(fh)
        except (OSError, ValueError):
            table = {}
        table.setdefault("_learned", {}).update({str(c): t for c, t in learned.items()})
        with open("commands.json", "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=1)
        for c, t in learned.items():
            CMD_TABLE[c] = (t, False)
        print("  learned: " + ", ".join(f"cmd {c} = {t}" for c, t in learned.items()) + "  (commands.json)")


def calibrate(sides, inj, facing_right, hot):
    """Press every button with every direction our keys can make, read the
    CommandCode and move id the game gives each one, and print the table.
    Answers two questions at once: what each cmd number means, and which
    directions actually register through SendInput."""
    me = sides["P1"]
    order = [(0, b) for b in ("P", "K", "PK", "HK", "S", "T")]
    for d in (6, 4, 2, 8, 3, 9, 1, 7):
        order += [(d, b) for b in ("P", "K", "PK", "HK", "S")]
    order += [(d, "T") for d in (6, 4, 2)]     # the throw-break inputs holdbot uses
    order += [("46", b) for b in ("PK", "P", "K")] + [("64", "PK"), ("236", "P"), ("214", "P")]
    order += [("6hb", "PK"), ("4hb", "P"), ("6hb", "K")]   # BUTTON held 1 s
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
        held_btn = isinstance(digit, str) and digit.endswith("hb")
        dstr = str(digit).rstrip("hb") if digit else ""
        motion = [int(ch) for ch in dstr[:-1]] if len(dstr) > 1 else []
        dnum = int(dstr[-1]) if dstr else 0
        for dg in motion:                     # 46P+K: tap 4, then 6 + button
            mdx, mdy = NUMPAD[dg]
            if not facing_right:
                mdx = -mdx
            nm = dirs_to_names(mdx, mdy)
            inj.down(nm); time.sleep(0.033); inj.up(nm); time.sleep(0.017)
        dx, dy = NUMPAD.get(dnum, (0, 0)) if dnum else (0, 0)
        if not facing_right:
            dx = -dx
        horiz, vert = dirs_to_names(dx, 0), dirs_to_names(0, dy)
        key = BUTTON_KEY[btn]
        if horiz:
            inj.down(horiz); time.sleep(0.35 if held else 0.017)
        inj.down(vert + [key]); time.sleep(1.0 if held_btn else 0.045)
        inj.up([key]); inj.up(vert + horiz)
        # the CommandCode flickers through direction codes (2, 6, 1349...)
        # on the way to the attack's own code: take the one in place on the
        # frame the move id appears, and keep every id the move goes through
        # (a charged 6P+K starts as 8427 and becomes 8428 while held)
        got_cmd, ids = None, []
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 0.9:
            me.refresh()
            c, m = me.get("CommandCode"), me.get("CurrentMove")
            if m not in IDLE_MOVES:
                if not ids:
                    got_cmd = int(c) if c else None
                if not ids or ids[-1] != m:
                    ids.append(int(m))
            elif ids:
                break
            time.sleep(0.002)
        tok = f"{digit if digit else ''}{btn}"
        table[tok] = {"cmd": got_cmd, "move": ids[0] if ids else None, "ids": ids}
        print(f"  {tok:<5} -> cmd {got_cmd}  move {'>'.join(map(str, ids)) if ids else None}")
        time.sleep(0.4)
    with open("commands.json", "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1)
    print("saved commands.json")


def probe(sides, inj, facing_right, hot, want_cmd, btn="PK"):
    """Try a list of input recipes for one button until the game answers
    with the wanted CommandCode. Memory is sampled WHILE the recipe runs
    (a move that starts and ends during a 1.5 s button hold was invisible
    to a sampler that only started afterwards)."""
    me = sides["P1"]
    key = BUTTON_KEY[btn]
    f, b = dirs_to_names(1 if facing_right else -1, 0), dirs_to_names(-1 if facing_right else 1, 0)
    log = {"cmds": [], "ids": []}

    def sample():
        me.refresh()
        c, m = me.get("CommandCode"), me.get("CurrentMove")
        if c and (not log["cmds"] or log["cmds"][-1] != c):
            log["cmds"].append(int(c))
        if m not in IDLE_MOVES and (not log["ids"] or log["ids"][-1] != m):
            log["ids"].append(int(m))

    def wait(sec):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < sec:
            sample()
            time.sleep(0.002)

    recipes = [
        ("4 tap, 6 + " + btn + " together",     lambda: (inj.down(b), wait(0.033), inj.up(b), wait(0.017), inj.down(f + [key]), wait(0.05), inj.up([key] + f))),
        ("4 tap, 6, " + btn + " a frame later", lambda: (inj.down(b), wait(0.033), inj.up(b), wait(0.017), inj.down(f), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("4 held 0.2s, 6 + " + btn,             lambda: (inj.down(b), wait(0.2), inj.up(b), wait(0.017), inj.down(f + [key]), wait(0.05), inj.up([key] + f))),
        ("4, neutral 3f, 6 + " + btn,           lambda: (inj.down(b), wait(0.033), inj.up(b), wait(0.05), inj.down(f + [key]), wait(0.05), inj.up([key] + f))),
        ("6 tap, 4 + " + btn,                   lambda: (inj.down(f), wait(0.033), inj.up(f), wait(0.017), inj.down(b + [key]), wait(0.05), inj.up([key] + b))),
        ("6 held 1.2s, then " + btn,           lambda: (inj.down(f), wait(1.2), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("4 held 1.2s, then " + btn,           lambda: (inj.down(b), wait(1.2), inj.down([key]), wait(0.05), inj.up([key] + b))),
        ("66 dash, then " + btn,               lambda: (inj.down(f), wait(0.03), inj.up(f), wait(0.03), inj.down(f), wait(0.05), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("6 + " + btn + " held 2.5s",          lambda: (inj.down(f), wait(0.017), inj.down([key]), wait(2.5), inj.up([key] + f))),
        (btn + " alone held 2.5s",             lambda: (inj.down([key]), wait(2.5), inj.up([key]))),
        ("4 + " + btn + " held 2.5s",          lambda: (inj.down(b), wait(0.017), inj.down([key]), wait(2.5), inj.up([key] + b))),
        ("6 held, " + btn + " tapped twice",   lambda: (inj.down(f), wait(0.1), inj.down([key]), wait(0.05), inj.up([key]), wait(0.15), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("left+right together + " + btn,       lambda: (inj.down(f + b), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + f + b))),
    ]
    print(f"probing for cmd {want_cmd} with {btn}. F10 aborts.")
    for name, do in recipes:
        if "F10" in hot.pressed():
            break
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 3.0:
            me.refresh()
            if me.get("CurrentMove") in IDLE_MOVES and me.get("MoveKind") == 0:
                break
            time.sleep(0.005)
        time.sleep(0.3)
        log["cmds"], log["ids"] = [], []
        do()
        inj.release_all()
        wait(1.5)                       # whatever comes out on release
        mark = "  <== MATCH" if want_cmd in log["cmds"] else ""
        print(f"  {name:<32} -> cmds {'>'.join(map(str, log['cmds'])) or None}  "
              f"moves {'>'.join(map(str, log['ids'])) or None}{mark}")
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-cmd", type=int, default=None,
                    help="try input recipes for --probe-btn until this CommandCode appears")
    ap.add_argument("--probe-btn", default="PK", choices=list(BUTTON_KEY))
    ap.add_argument("--calibrate", action="store_true",
                    help="press every input once and print the cmd / move it produces")
    ap.add_argument("--process", default=None)
    ap.add_argument("--me", default="P1", choices=["P1", "P2"])
    ap.add_argument("--facing", default="right", choices=["right", "left"],
                    help="which way our character faces at the start of the combo")
    ap.add_argument("--lag-frames", type=int, default=2,
                    help="press this many frames earlier than the demo did (input lag)")
    args = ap.parse_args()

    load_commands()
    if CMD_TABLE:
        print(f"commands.json: {len(CMD_TABLE)} command codes known")
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
    if args.probe_cmd:
        probe(sides, inj, facing_right, hot, args.probe_cmd, args.probe_btn)
        inj.release_all()
        return

    steps = None
    while True:
        events, me = record(sides, hot, rebind)
        if events is None:
            break
        steps = plan(events)
        tries = {}
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
                replay(me, steps, inj, facing_right, args.lag_frames, tries)
                print("F8 replay   F5 record again   F7 save   F10 quit")
            time.sleep(0.01)


if __name__ == "__main__":
    main()
