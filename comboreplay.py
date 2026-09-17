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
VK = {"F5": 0x74, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79}

# P 1000 / K 1100 seen in matches; 5700 is P+K (a Combo Challenge demo of
# 8P+K read cmd 5780 and produced move 8428 - the 84xx ids are the P+K
# family seen from pokes). H+K and S bases are still unknown.
BUTTON_BASE = {1000: "P", 1100: "K", 5700: "PK"}
BUTTON_KEY = {"P": "punch", "K": "kick", "PK": "pk", "HK": "hk", "S": "special",
              "T": "throw", "H": "free"}
NUMPAD = {1: (-1, -1), 2: (0, -1), 3: (1, -1), 4: (-1, 0), 5: (0, 0),
          6: (1, 0), 7: (-1, 1), 8: (0, 1), 9: (1, 1)}
IDLE_MOVES = (0, 1, 2, 3, 4)
CC_FILE = "combo_challenge.json"   # {char: ["HK,P,P,P,P", ...]} cleared stages
NEUTRAL_IDS = set()   # animation ids this character shows while MoveKind is 0
HELD_FWD = []         # forward left down by close_in so the next move carries it
CHAR_TABLE = [False]  # this character's own calibration is loaded
DOWNED = tuple(range(70, 100)) + tuple(range(125, 140))   # lying down / getting up


def neutral(side):
    """Is nothing of ours playing right now?

    "CurrentMove in 0..4" is not the test. Minato DANCES on the spot: her
    neutral cycles through 8019/8023/8027/8030/8034, so that check was never
    true, the calibration timed out waiting for her to stand still, pressed
    anyway and recorded the tail of the previous move (K read as
    8023>179>8030 with cmd 1903). MoveKind 0 is what actually means neutral,
    walking included."""
    mv = side.get("CurrentMove")
    if side.get("MoveKind") == 0:
        NEUTRAL_IDS.add(int(mv))
        return True
    return False


def live(side):
    """A move of ours is playing (the inverse of neutral, id-aware so a move
    that reads MoveKind 0 for a frame still counts)."""
    mv = side.get("CurrentMove")
    return not neutral(side) and mv not in IDLE_MOVES and mv not in NEUTRAL_IDS


CMD_TABLE = {}      # cmd -> (token, button held)  from commands.json (--calibrate)
MOVE_TABLE = {}     # move id -> token that produced it in the calibration
HINTS = {}          # cmd -> token to try FIRST (read off the stage's task list)


def _apply_commands(table):
    """The calibration table is the truth: P+K is 1210 neutral / 5770 with
    6 / 1230 with 4, 4K is 1540, 1P is 1014 - no formula gives those. A cmd
    exactly 10 above a known one is that input with the BUTTON HELD (the
    stage that asked for "hold P+K" read 5780 where 6P+K reads 5770)."""
    for tok, v in table.items():
        if tok.startswith("_") or tok == "chars" or not isinstance(v, dict):
            continue
        for m in ([v.get("move")] if v.get("move") else []) + list(v.get("ids") or []):
            if m and (m not in MOVE_TABLE or len(tok) < len(MOVE_TABLE[m])):
                MOVE_TABLE[m] = tok.replace("h", "")
        c = v.get("cmd")
        if not c:
            continue
        cur = CMD_TABLE.get(c)
        rank = (tok.endswith("h") or "h" in tok[:-2], len(tok))
        if cur is None or rank < (cur[0].endswith("h") or "h" in cur[0][:-2], len(cur[0])):
            CMD_TABLE[c] = (tok.replace("h", ""), False)
    # codes learned by replaying (cmd -> token that produced the wanted move)
    for c_s, tok in table.get("_learned", {}).items():
        CMD_TABLE[int(c_s)] = (tok, False)
    for c_s, tok in table.get("_hints", {}).items():
        HINTS[int(c_s)] = tok
    # (the "+10 = button held" rule is gone: 5510 is the second S of the
    # Fatal Rush, 5780 is the 46P+K motion)


def read_commands(path="commands.json"):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def load_commands(path="commands.json", char=None):
    """The calibration table is the truth: P+K is 1210 neutral / 5770 with
    6 / 1230 with 4, 4K is 1540, 1P is 1014 - no formula gives those.

    Codes are mostly the input, not the character, so the shared table at the
    top level is applied first; anything under chars/<id> then overrides it,
    because a code CAN mean different things (5500 is Nyotengu's Fatal Rush)
    and calibrating a second character must not delete the first one."""
    table = read_commands(path)
    if not table:
        return
    sec = (table.get("chars") or {}).get(str(char)) if char is not None else None
    if isinstance(sec, dict) and [k for k in sec if not k.startswith("_")]:
        # Codes are NOT shared across characters. Minato's Combo Challenge
        # used cmd 1320, which is absent from her table, so the shared one
        # (Nyotengu's calibration) answered "2HK" and the replay pressed a
        # move she does not have. Her own table is the only truth; anything
        # missing from it stays unknown and goes to the candidate search.
        CMD_TABLE.clear()
        MOVE_TABLE.clear()
        HINTS.clear()
        _apply_commands(sec)
        CHAR_TABLE[0] = True
        print(f"  commands.json: character {char} only "
              f"({len([k for k in sec if not k.startswith('_')])} inputs; the "
              f"shared table is not used - codes differ per character)")
    else:
        _apply_commands(table)


def distance(me, foe):
    try:
        mx, _, mz = me.xyz()
        fx, _, fz = foe.xyz()
        d = ((mx - fx) ** 2 + (mz - fz) ** 2) ** 0.5
        return d if 0 < d < 2000 else None
    except Exception:
        return None


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
    if CHAR_TABLE[0]:
        # the formulas below were read off Nyotengu (her K band starts at
        # 1100); Minato's K is 1220, so cmd 1190 decoded to "9K" and the
        # replay pressed a move she does not have. With her own table loaded,
        # anything missing from it is unknown, not guessed.
        return None
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
# probe recipe name -> the token that means the same thing
RECIPE_TOKENS = {"236": "236", "214": "214", "33": "33", "22": "22", "44": "44",
                 "41236": "41236", "2": "2", "8": "8", "1": "1", "3": "3"}
FAMILY = {10: "P", 11: "K", 55: "S", 50: "S", 57: "PK"}   # 13xx / 20xx were stance and hit follow-ups


def candidates(cmd, want_mv=None, in_throw=False, after_walk=False, follow_up=False):
    """What to try for a code we never produced ourselves. The demo of one
    stage showed the hundreds are NOT a reliable button family (1083 and
    1085 were P+K after a hit, 1350-1353 P / K inside a stance), so: the
    calibration token that produced the wanted move id if we have one, then
    every button, the code's own family first."""
    out = []
    if cmd in HINTS:
        out.append(HINTS[cmd])
    if want_mv in MOVE_TABLE and MOVE_TABLE[want_mv] not in out:
        out.append(MOVE_TABLE[want_mv])
    # A THROW code was unanswerable: "T" was not in the list at all, so
    # Minato's stage-1 cmd 403 could only ever be tried as P, K, P+K... The
    # calibration numbers every throw 400-404 (T, 6T, 4T, 2T) and holdbot has
    # seen 363-386 and 1349 from the CPU, so treat that band as throws.
    if in_throw:
        # a follow-up pressed while the throw plays: T continues a throw
        # chain, S is the Fatal Rush pattern, then the plain buttons
        for b in ("T", "S", "P", "K", "PK", "HK", "6T", "4T", "2T"):
            if b not in out:
                out.append(b)
    if 350 <= cmd <= 420 or cmd in (1349, 1500, 1501):
        for b in ("T", "6T", "4T", "2T", "3T", "1T", "9T", "7T"):
            if b not in out:
                out.append(b)
    if after_walk:
        # the demo stepped forward and then attacked: that is how a dash move
        # is buffered, and none of 66P / 66K was ever in the list
        for b in ("66P", "66K", "66PK", "66HK", "33P", "33K"):
            if b not in out:
                out.append(b)
    if follow_up:
        # A string continuation is numbered in its own space: PPPP runs
        # 1000/1001/1002/1003, and Minato's PPP>4P read 5701 while her
        # standalone 4P is 1150. The nearest calibrated code says nothing
        # here, so go through the branches a string can actually take.
        for b in ("P", "6P", "4P", "2P", "8P", "K", "6K", "4K", "2K", "8K",
                  "PK", "6PK", "2PK", "HK", "S", "3K", "3P"):
            if b not in out:
                out.append(b)
    # The nearest calibrated code is the best guide to the button: Minato's
    # K sits at 1220, 6K at 1280, 4K at 1300, 2K at 1310, so cmd 1320 is a
    # kick of some sort and nothing else is worth trying first.
    if CMD_TABLE:
        gap, near = min((abs(c - cmd), c) for c in CMD_TABLE)
        if gap <= 40:
            btn_n = CMD_TABLE[near][0].lstrip("0123456789")
            for d in ("", "2", "6", "4", "8", "3", "1", "9", "7"):
                c_tok = f"{d}{btn_n}"
                if c_tok not in out:
                    out.append(c_tok)
    fam = FAMILY.get(cmd // 100)
    first = ["6S", "S"] if fam == "S" else ([fam] if fam else [])   # 6S: the Break Blow (8381)
    # H: the Break Blow's follow-up on the stage screen was "-> S  H";
    # 2P: the "down P on hit" task read cmd 2082, not the calibration's 1020
    for b in first + ["P", "PK", "K", "HK", "S", "H", "2P", "6P", "4P", "2K"]:
        if b not in out:
            out.append(b)
    for btn_l in ("K", "P", "PK", "HK"):          # nothing left but brute force
        for d in ("2", "6", "4", "8", "3", "1", "9", "7", "66", "33", "236", "214"):
            if f"{d}{btn_l}" not in out:
                out.append(f"{d}{btn_l}")
    digit = (cmd % 100) // 10
    if fam and digit and digit in NUMPAD and digit != 5 and f"{digit}{fam}" not in out:
        out.append(f"{digit}{fam}")
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
                # MoveKind 3/16/5 covered the characters seen first, but
                # Minato's opening move reads kind 2 (move 8130) and the
                # recorder waited forever. Any NON-ZERO kind is a move of
                # ours; kind 0 is neutral, walking and - for Minato - her
                # dance, whose echo codes run past 1000 and started the
                # recording on move 8025 when the code alone was trusted.
                if st[0] not in IDLE_MOVES and st[1]:
                    me = sd
                    foe = [o for n, o in sides.items() if n != name][0]
                    started = now
                    last_cmd, last_mv = st[2], None
                    print(f"  demo started on {name}")
                    if st[2]:
                        # the first command is already in place when we
                        # notice the demo: record it (a one-move stage has
                        # nothing else, and the first recording had 0 inputs)
                        d0 = distance(me, foe)
                        events.append({"t": 0.0, "cmd": int(st[2]), "tok": token(st[2]),
                                       "prev_mv": 0, "prev_fr": 0,
                                       "dist": None if d0 is None else round(d0)})
                        print(f"   0.000s  input {token(st[2]):<6} (cmd {st[2]})  first")
                    break
            if started is None:
                time.sleep(0.002)
                continue
        me.refresh(); foe.refresh()
        cmd, mv, fr = me.get("CommandCode"), me.get("CurrentMove"), me.get("CurrentMoveFrame")
        kind_now = me.get("MoveKind")
        # While a throw executes (MoveKind 4) the game writes its own numbers
        # into CommandCode, in lockstep with the animation: Minato's stage 1
        # read as cmd 2030/2032/2034 against moves 8156/8158/8160, both
        # climbing by 2, and the replay then tried to press four inputs for
        # what is one throw. Nothing can be input during a throw anyway.
        # A code that arrives with the character back in neutral (kind 0) is
        # the same kind of echo.
        # A code arriving during a throw (MoveKind 4) looked like an echo of
        # the animation - it always lands on frame 1, in lockstep with the
        # move id. But a buffered follow-up input is consumed on exactly that
        # frame too, and the decisive evidence is the replay: press 214T alone
        # and the throw stops at 8156, while the demo runs on to 8158 and
        # 8160. So they are inputs, and they are recorded like any other.
        # What IS an echo is the code that arrives as we drop back into the
        # dance (MoveKind 0 on a neutral id).
        if kind_now == 0 and mv not in IDLE_MOVES and cmd != last_cmd:
            NEUTRAL_IDS.add(int(mv))     # dropping back into the dance
            last_cmd = cmd
        if cmd != last_cmd and cmd:
            d_in = distance(me, foe)
            events.append({"t": round(now - started, 3), "cmd": int(cmd), "tok": token(cmd),
                           "prev_mv": int(last_mv) if last_mv is not None else int(mv),
                           "prev_fr": int(fr), "in_throw": kind_now == 4,
                           "dist": None if d_in is None else round(d_in)})
            print(f"  {now - started:6.3f}s  input {token(cmd):<6} (cmd {cmd})  "
                  f"during move {last_mv if last_mv is not None else mv} frame {fr}"
                  + (f"  dist {d_in:.0f}" if d_in is not None else ""))
        if mv != last_mv:
            events.append({"t": round(now - started, 3), "mv": int(mv), "kind": int(me.get("MoveKind"))})
            print(f"  {now - started:6.3f}s  move {mv} (kind {me.get('MoveKind')})")
            last_mv = mv
        last_cmd = cmd
        both_idle = (neutral(me) and (neutral(foe)
                     or foe.get("CurrentMove") in (127, 131, 135)))
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
    t_prev = None
    for i, e in enumerate(events):
        if "cmd" not in e:
            continue
        dt = None if t_prev is None else round(e["t"] - t_prev, 3)
        t_prev = e["t"]
        produced, chain = None, []
        for f in events[i + 1:]:
            if "mv" in f and f["mv"] not in IDLE_MOVES:
                if produced is None:
                    produced = f["mv"]
                chain.append(f["mv"])
                continue
            if "cmd" in f:
                break
        steps.append({"tok": e["tok"], "cmd": e["cmd"], "prev_mv": e["prev_mv"],
                      "prev_fr": e["prev_fr"], "mv": produced, "dist": e.get("dist"),
                      "dt": dt, "chain": chain, "in_throw": e.get("in_throw", False)})
    return steps


def press(inj, tok_cmd, facing_right, hold=0.045, tok=None, dash_hold=0.13,
          dir_lead=0.017):
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
    dash = len(digits) >= 2 and digits[-1] == digits[-2]
    if dash and horiz:
        # A dash carries the move with it, and the run-up is most of the
        # range. The demo held forward for 0.14 s before pressing; tapping
        # and pressing 17 ms later produced the right move (8077) barely a
        # step from where it started, and the 6P fell short of the post.
        inj.down(horiz)
        time.sleep(dash_hold)
        inj.down(vert + [key])
    elif horiz and vert and dy < 0:
        # A DOWN diagonal needs both directions in place before the button.
        # With the vertical sent alongside the button the game kept only the
        # horizontal: Minato's 3K came out as 6K (8087) and 1P/7P as 4P, while
        # the UP diagonals (9P 188, 9K 189) were fine. Combo Challenge stage 6
        # opens with 3K, move 180, which no other input produces.
        inj.down(horiz + vert)
        time.sleep(0.05)
        inj.down([key])
    else:
        if horiz:
            inj.down(horiz)
            time.sleep(dir_lead)
        inj.down(vert + [key])
    time.sleep(hold)
    inj.up([key])
    inj.up(vert + horiz)
    return True


MOVE_CMDS = {9: "66", 4: "44"}      # dashes seen in a demo (moves 3 / 5)


def close_in(me, foe, inj, facing_right, want, timeout=2.5, keep=False):
    """Walk forward until we are as close as the demo was (+10) for this
    input. The dummy is a post that never moves; the demo dashed in before
    the close-range tasks and our replay stood where it was, so half the
    moves whiffed. The walk id tells us if 'forward' is mirrored."""
    d = distance(me, foe)
    if d is None or want is None or d <= want + 3:
        return d
    fwd = dirs_to_names(1 if facing_right else -1, 0)
    inj.down(fwd)
    t0 = time.perf_counter()
    d0 = d
    flipped = False
    d_start, t_gain = d, time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        me.refresh()
        mv = me.get("CurrentMove")
        if d is not None and d_start - d > 5:
            d_start, t_gain = d, time.perf_counter()
        elif time.perf_counter() - t_gain > 0.6:
            break                                # not getting closer: give up
        if not flipped and mv in (2, 4) and time.perf_counter() - t0 > 0.06:
            inj.up(fwd)                          # walking away: mirrored
            fwd = dirs_to_names(-1 if facing_right else 1, 0)
            inj.down(fwd)
            flipped = True
        if (not flipped and d0 is not None and time.perf_counter() - t0 > 0.4
                and d is not None and d > d0 - 8):
            inj.up(fwd)                          # 0.4 s of walking and no
            fwd = dirs_to_names(-1 if facing_right else 1, 0)   # ground gained:
            inj.down(fwd)                        # we are walking away from it
            flipped = True
        d = distance(me, foe)
        if d is not None and d <= want + 3:
            break
        time.sleep(0.005)
    if keep:
        # The demo pressed WHILE still walking in, and a move thrown out of a
        # forward walk travels with it. Letting go and standing still for a
        # tenth of a second first cost exactly that ground, and the 6P at the
        # end of stage 6 fell short. Forward stays down; the caller's press
        # releases it.
        HELD_FWD[:] = fwd
    else:
        HELD_FWD.clear()
        inj.up(fwd)
        time.sleep(0.1)                          # neutral, or the button reads 6P
    if d is not None and d > want + 3:
        print(f"      (closing in stopped at {d:.0f}, wanted {want}: "
              f"{'walked the wrong way, flipped' if flipped else 'the walk gained nothing - touching the post?'})")
    return d


def replay(me, steps, inj, facing_right, lag_frames=2, tries=None, foe=None):
    """Play the steps back. A step the demo made from idle (prev move 0)
    waits for OUR idle; a string follow-up waits for the previous produced
    move and its frame. Unknown codes go through the family candidates,
    one per replay, and a candidate that produces the wanted move id is
    saved to commands.json so the next replay knows it."""
    tries = tries if tries is not None else {}
    print(f"replaying {len(steps)} input(s): " + " ".join(s["tok"] for s in steps))
    me.refresh()
    results, learned, misses, step_tokens = [], {}, [], []
    t_prev_press = time.perf_counter()
    for i, s in enumerate(steps):
        from_idle = s["prev_mv"] in IDLE_MOVES and not s.get("in_throw")
        if s["cmd"] in MOVE_CMDS or s["cmd"] in (135, 60) or s["mv"] is None:
            # the demo's own steps toward the post: replaced by close_in()
            step_tokens.append(None)
            print(f"  {i + 1:>2}. {s['tok']:<10} (the demo walking in - handled by closing in)")
            continue
        if from_idle:
            # BOTH of us, and the first step too. This wait used to be inside
            # "if i > 0", so a one-input stage fired the instant F8 was
            # pressed - with the post still lying where the last replay threw
            # it. A throw onto a body that is not standing plays a short
            # version: the demo ran 8141>8156>8158>8160 and ours stopped at
            # 8156.
            t0 = time.perf_counter()
            # Only the opening input waits for the post to be back on its feet.
            # Later on the demo attacks the moment IT is free - waiting for the
            # post too put the third part of stage 6 a long way behind.
            want_up = i == 0
            while time.perf_counter() - t0 < (4.0 if want_up else 1.2):
                me.refresh()
                if foe is not None:
                    foe.refresh()
                up = (not want_up) or foe is None or (
                    neutral(foe) and foe.get("CurrentMove") not in DOWNED)
                if neutral(me) and up:
                    break
                time.sleep(0.002)
            time.sleep(0.05 if want_up else 0.0)
            if want_up and foe is not None and (not neutral(foe)
                                                or foe.get("CurrentMove") in DOWNED):
                print(f"      (the post is still busy: move {foe.get('CurrentMove')} "
                      f"kind {foe.get('MoveKind')} - pressing anyway)")
        if i > 0:
            t0 = time.perf_counter()
            if from_idle:
                pass
            else:
                # The demo's "frame 1" is the game pre-loading its own next
                # command; the real timing is the interval between the two
                # inputs (H+K -> P+K came 0.39 s later, when the kick hit;
                # the Break Blow's H 1.05 s later). Press from a bit before
                # that interval, re-pressing until well after it.
                want_mv = steps[i - 1]["mv"]
                dt = s.get("dt") or 0.0
                # only just ahead of the demo's interval: P+K 0.15 s before
                # the H+K hit came out as a plain P+K (8119), not the stance.
                # Once a press has worked, use its timing
                known = tries.get(("t", cmd, s["mv"]))
                lead = (max(0.0, known - 0.02) if known is not None
                        else max(0.0, dt - 0.05 - lag_frames / 60))
                while time.perf_counter() - t0 < max(1.0, dt + 0.3):
                    me.refresh()
                    mv = me.get("CurrentMove")
                    since = time.perf_counter() - t_prev_press
                    if since >= lead and (want_mv is None or mv == want_mv or mv not in IDLE_MOVES):
                        break
                    if neutral(me) and since > max(0.25, dt + 0.1):
                        break               # the string dropped: press anyway
                    time.sleep(0.001)
        d_now = None
        if from_idle and foe is not None:
            # the demo was walking when it pressed: keep walking into the move
            keep_fwd = s["prev_mv"] in (1, 3, 5, 6)
            d_now = close_in(me, foe, inj, facing_right, s.get("dist"),
                             timeout=2.5 if i == 0 else max(0.4, (s.get("dt") or 0.5)),
                             keep=keep_fwd)
        cmd = s["cmd"]
        used = None
        t_prev_press = time.perf_counter()
        if HELD_FWD:
            hf = list(HELD_FWD)
            HELD_FWD.clear()
        else:
            hf = []
        # a dash follows the demo's own run-up length
        dh = max(0.06, min(0.30, s.get("dt") or 0.13)) if s["prev_mv"] in (1, 3, 5, 6) else 0.13
        # A direction inside a string needs longer than the 17 ms that works
        # from neutral: PPP>4P came out as the plain fourth P (8047), the back
        # having been dropped, and that broke the 6P after it as well.
        dl = 0.017 if from_idle else 0.05
        if decode(cmd) is not None:
            ok = press(inj, cmd, facing_right, dash_hold=dh, dir_lead=dl)
            used = token(cmd)
        elif cmd in MOVE_CMDS:
            used = MOVE_CMDS[cmd]
            d = 1 if (used == "66") == facing_right else -1
            names = dirs_to_names(d, 0)
            inj.down(names); time.sleep(0.033); inj.up(names); time.sleep(0.017)
            inj.down(names); time.sleep(0.05); inj.up(names)
            ok = True
        else:
            cands = candidates(cmd, s["mv"], s.get("in_throw", False),
                               after_walk=s["prev_mv"] in (1, 3, 5, 6),
                               follow_up=not from_idle)
            prev_tok = step_tokens[-1] if step_tokens else None
            if (prev_tok and i and cmd == steps[i - 1]["cmd"] + 1
                    and tries.get((cmd, s["mv"]), 0) == 0):
                cands = [prev_tok] + [c for c in cands if c != prev_tok]
            if s.get("in_throw") and tries.get("_throw_tok"):
                # part 2 of Minato's 214T was 214T again: a multi-part throw
                # repeats its own input, so whatever carried the last part is
                # the first thing to try for this one
                t_rep = tries["_throw_tok"]
                cands = [t_rep] + [c for c in cands if c != t_rep]
            if cands and tries.get(("_said", cmd)) is None:
                tries[("_said", cmd)] = True
                print(f"      cmd {cmd} is not in commands.json - trying, in order: "
                      + " ".join(cands[:8]) + "  (--calibrate names it for good)")
            if cands:
                n = tries.get((cmd, s["mv"]), 0)
                used = cands[n % len(cands)]
                # a follow-up whose previous step did not come out was never
                # really tested: keep the same candidate next time
                prev_ok = from_idle or (results and results[-1][1] is not None
                                        and results[-1][1] in results[-1][2])
                if prev_ok:
                    tries[(cmd, s["mv"])] = n + 1
                ok = press(inj, cmd, facing_right, tok=used, dash_hold=dh, dir_lead=dl)
            else:
                ok = False
        if hf:
            inj.up(hf)
        ids = []
        prev = steps[i - 1]["mv"] if i else None
        t1 = time.perf_counter()
        # a follow-up that needs the previous move to HIT first (H+K -> P+K
        # into the stance) can only be taken once the hit lands, ~0.4 s
        # into a moving attack: keep re-pressing until the previous move
        # ends, not for a fixed 0.35 s
        limit = (0.35 if from_idle else max(0.9, (s.get("dt") or 0) + 0.5)) if i + 1 < len(steps) else 1.2
        if len(s.get("chain") or []) > 1:
            limit = max(limit, 3.0)      # a throw runs for seconds
        again, t_last = 0, time.perf_counter()
        presses = [time.perf_counter() - t_prev_press]
        # once a follow-up's timing is known, one press (plus one spare):
        # re-pressing P in the stance queued a second P, and the K that
        # followed came out as P,P (8264) instead of the stance kick
        max_again = 1 if tries.get(("t", cmd, s["mv"])) is not None else 10
        landed = False
        while time.perf_counter() - t1 < limit:
            me.refresh()
            if foe is not None:
                foe.refresh()
                fm = foe.get("CurrentMove")
                if foe.get("MoveType") == 3 or 24000 <= fm < 27000 or 16000 <= fm < 16100:
                    landed = True                # hit (or blocked) reaction on the post
            mv = me.get("CurrentMove")
            if mv not in IDLE_MOVES and mv != prev:
                if not ids or ids[-1] != mv:
                    ids.append(int(mv))
                if s["mv"] in ids and i + 1 < len(steps):
                    break
            elif ids:
                break
            elif (not from_idle and neutral(me)
                  and time.perf_counter() - t1 > 0.2):
                break                        # the previous move ended: follow-up missed
            elif (ok and used and not from_idle and again < max_again
                  and time.perf_counter() - t_last > (0.12 if max_again == 1 else 0.07)):
                # a string follow-up the game did not take yet: the demo's
                # "frame 1" is the game's own pre-loaded command, not a
                # human timing. Press again every ~4 frames (holdbot's
                # combo engine lands its strings this way)
                press(inj, cmd, facing_right, tok=used if decode(cmd) is None else None,
                      dash_hold=dh, dir_lead=dl)
                again += 1
                t_last = time.perf_counter()
                presses.append(t_last - t_prev_press)
            time.sleep(0.001)
        hit = s["mv"] is not None and s["mv"] in ids
        if hit and not from_idle and ("t", cmd, s["mv"]) not in tries:
            tries[("t", cmd, s["mv"])] = presses[-1]     # the press that worked
        if hit and decode(cmd) is None and used and used not in MOVE_CMDS.values():
            learned[cmd] = used
        if hit and s.get("in_throw") and used:
            tries["_throw_tok"] = used
        elif hit and s.get("chain") and decode(cmd) is not None:
            tries["_throw_tok"] = token(cmd)
        if not hit and s["mv"] is not None and ids:
            tries.setdefault("_miss", []).append((s["tok"], used, ids[0]))
        if not hit and used and decode(cmd) is None:
            misses.append((s["tok"], cmd, s["mv"], used))
        results.append((s["tok"], s["mv"], ids))
        step_tokens.append(token(cmd) if decode(cmd) is not None else used)
        print(f"  {i + 1:>2}. {s['tok']:<10} wanted move {s['mv']}  got "
              f"{'>'.join(map(str, ids)) if ids else None}"
              + (f"  tried {used}" if decode(cmd) is None and used else "")
              + (f"  (+{again} re-press)" if again else "")
              + (f"  dist {d_now:.0f}/{s['dist']}" if d_now is not None and s.get("dist") else "")
              + ("  HIT" if landed else "")
              + ("" if ok else "  (unknown code, nothing pressed)")
              + ("  OK" if hit else ""))
        # Only when this step actually missed. A long animation often spills
        # its later ids into the NEXT step's window (the Fatal Rush's 8257 was
        # followed by 8258 under the next press), and warning there was a
        # false alarm on a step that matched.
        want_chain = [m for m in (s.get("chain") or []) if m not in NEUTRAL_IDS]
        got_chain = [m for m in ids if m not in NEUTRAL_IDS]
        if not hit and len(want_chain) > 1 and len(got_chain) < len(want_chain):
            print(f"      the demo ran {'>'.join(map(str, want_chain))}, ours stopped "
                  f"at {'>'.join(map(str, got_chain)) or 'nothing'}"
                  + (f" (dist {d_now:.0f} vs the demo's {s.get('dist')})"
                     if d_now is not None and s.get("dist") else "")
                  + " - it ran short: out of range, or a later part needs its own input")
    hits = sum(1 for _, w, g in results if w is not None and w in g)
    print(f"  {hits}/{len(results)} moves matched the demonstration")
    # A stage the replay clears is a combo the GAME itself teaches. Save the
    # token sequence so holdbot can try it in a match: its bandit scores it by
    # net damage against everything else in the character's pool.
    if hits and hits == len(results) and len(results) > 1:
        seq = [tk for tk in step_tokens if tk]
        # Everything is saved, throws included: holdbot skips a throw-led
        # sequence when picking a combo recipe (a character in hit stun cannot
        # be grabbed) and uses the longest one where it has decided to throw
        # anyway. A single move is a valid recipe too - the pools already
        # carry "S" and "P" as one-token entries.
        if seq:
            text = ",".join(seq)
            ch = me.get("CurrentCharacter")
            try:
                with open(CC_FILE, encoding="utf-8") as fh:
                    cc = json.load(fh)
            except (OSError, ValueError):
                cc = {}
            lst = cc.setdefault(str(ch), [])
            if text not in lst:
                lst.append(text)
                with open(CC_FILE, "w", encoding="utf-8") as fh:
                    json.dump(cc, fh, indent=1, sort_keys=True)
                print(f"  saved \"{text}\" to {CC_FILE} - holdbot will try it "
                      f"for character {ch}")
            else:
                print(f"  \"{text}\" is already in {CC_FILE}")
    for nm, cmd_m, mv_m, used_m in misses:
        cands = candidates(cmd_m, mv_m)
        if cands:
            nxt = cands[tries.get((cmd_m, mv_m), 0) % len(cands)]
            print(f"      {nm}: {used_m} was wrong - next is {nxt} "
                  f"(F8 once, or F9 to run through them)")
    return hits, len(results)
    if learned:
        table = read_commands()
        char = me.get("CurrentCharacter")
        where = (table.setdefault("chars", {}).setdefault(str(char), {})
                 if char is not None else table)
        where.setdefault("_learned", {}).update({str(c): t for c, t in learned.items()})
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
    # every direction with T: holdbot needs the break inputs, and a Combo
    # Challenge stage can be a single directional throw (Minato's first is
    # cmd 403, which none of 400/401/402/404 covered)
    order += [(d, "T") for d in (6, 4, 2, 8, 3, 9, 1, 7)]
    order += [("46", b) for b in ("PK", "P", "K")] + [("64", "PK"), ("236", "P"), ("214", "P")]
    order += [(d, "T") for d in ("236", "214", "46", "64", "66", "44", "33", "22")]
    order += [("6hb", "PK"), ("4hb", "P"), ("6hb", "K")]   # BUTTON held 1 s
    # "hold the direction" commands (the screen said hold left/right + P+K
    # for the move the demo read as cmd 5780): direction held 0.35 s first
    for d in (6, 4):
        order += [(f"{d}h", b) for b in ("P", "K", "PK", "HK", "S")]
    char = me.get("CurrentCharacter")
    table = {}
    print(f"calibrating character {char}: stand idle in the game and do not "
          "touch the keys "
          f"({len(order)} inputs, ~1 s each). F10 aborts.")
    time.sleep(1.0)       # the first input read None when it went out too early
    for digit, btn in order:
        if "F10" in hot.pressed():
            break
        # wait until we are idle again
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 3.0:
            me.refresh()
            if neutral(me):
                break
            time.sleep(0.005)
        time.sleep(0.25)
        me.refresh()
        if not neutral(me):          # still busy: the reading would be the
            time.sleep(0.5)          # tail of the last move, give it longer
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
        inj.down(vert + [key])
        # Sample from the frame the button goes DOWN, not after the release.
        # The held variants hold the button for a second and the direction for
        # a third: waiting for the release meant the move had already played
        # and ended, and all thirteen of them recorded "cmd None move None"
        # even though they visibly came out.
        hold_for = 1.0 if held_btn else 0.045
        t_press = time.perf_counter()
        released = False
        # the CommandCode flickers through direction codes (2, 6, 1349...)
        # on the way to the attack's own code: take the one in place on the
        # frame the move id appears, and keep every id the move goes through
        # (a charged 6P+K starts as 8427 and becomes 8428 while held)
        got_cmd, ids = None, []
        limit = 1.8 if (held_btn or held) else 0.9
        while time.perf_counter() - t_press < limit:
            if not released and time.perf_counter() - t_press >= hold_for:
                inj.up([key]); inj.up(vert + horiz)
                released = True
            me.refresh()
            c, m = me.get("CommandCode"), me.get("CurrentMove")
            if live(me):
                if not ids:
                    got_cmd = int(c) if c else None
                if not ids or ids[-1] != m:
                    ids.append(int(m))
            elif ids and released:
                break
            time.sleep(0.002)
        if not released:
            inj.up([key]); inj.up(vert + horiz)
        tok = f"{digit if digit else ''}{btn}"
        table[tok] = {"cmd": got_cmd, "move": ids[0] if ids else None, "ids": ids}
        print(f"  {tok:<5} -> cmd {got_cmd}  move {'>'.join(map(str, ids)) if ids else None}")
        time.sleep(0.4)
    whole = read_commands()
    if not [k for k in whole if not k.startswith("_") and k != "chars"]:
        whole.update(table)            # first ever calibration: it IS the shared table
    whole.setdefault("chars", {}).setdefault(str(char), {}).update(table)
    with open("commands.json", "w", encoding="utf-8") as fh:
        json.dump(whole, fh, indent=1)
    print(f"saved commands.json (character {char} section; the other "
          f"characters' entries were kept)")


def probe(sides, inj, facing_right, hot, want_cmd, btn="PK"):
    """Try a list of input recipes for one button until the game answers
    with the wanted CommandCode. Memory is sampled WHILE the recipe runs
    (a move that starts and ends during a 1.5 s button hold was invisible
    to a sampler that only started afterwards)."""
    me = sides["P1"]
    key = BUTTON_KEY[btn]
    f, b = dirs_to_names(1 if facing_right else -1, 0), dirs_to_names(-1 if facing_right else 1, 0)
    log = {"cmds": [], "ids": []}
    saved = False

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
        ("2 + " + btn + " together",           lambda: (inj.down(["down", key]), wait(0.05), inj.up(["down", key]))),
        ("2 held 0.3s, then " + btn,           lambda: (inj.down(["down"]), wait(0.3), inj.down([key]), wait(0.05), inj.up(["down", key]))),
        ("1 (back+down) + " + btn,             lambda: (inj.down(b), wait(0.017), inj.down(["down", key]), wait(0.05), inj.up(["down", key] + b))),
        ("3 (fwd+down) + " + btn,              lambda: (inj.down(f), wait(0.017), inj.down(["down", key]), wait(0.05), inj.up(["down", key] + f))),
        ("66 dash, 2 + " + btn,                lambda: (inj.down(f), wait(0.03), inj.up(f), wait(0.03), inj.down(f), wait(0.15), inj.up(f), wait(0.017), inj.down(["down", key]), wait(0.05), inj.up(["down", key]))),
        ("8 + " + btn + " together",           lambda: (inj.down(["up", key]), wait(0.05), inj.up(["up", key]))),
        ("run (66 held 0.7s), then 2 + " + btn, lambda: (inj.down(f), wait(0.03), inj.up(f), wait(0.03), inj.down(f), wait(0.7), inj.up(f), inj.down(["down", key]), wait(0.05), inj.up(["down", key]))),
        ("run (66 held 0.7s), then " + btn,     lambda: (inj.down(f), wait(0.03), inj.up(f), wait(0.03), inj.down(f), wait(0.7), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("run, 2 + " + btn + " while running",  lambda: (inj.down(f), wait(0.03), inj.up(f), wait(0.03), inj.down(f), wait(0.7), inj.down(["down", key]), wait(0.05), inj.up(["down", key] + f))),
        ("left+right together + " + btn,       lambda: (inj.down(f + b), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + f + b))),
        # quarter circles: Minato's only special motion is 236P, so a command
        # throw of hers is most likely 236T or 214T
        ("236 + " + btn,                       lambda: (inj.down(["down"]), wait(0.033), inj.up(["down"]), inj.down(["down"] + f), wait(0.033), inj.up(["down"]), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + f))),
        ("214 + " + btn,                       lambda: (inj.down(["down"]), wait(0.033), inj.up(["down"]), inj.down(["down"] + b), wait(0.033), inj.up(["down"]), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + b))),
        ("33 (fwd-down twice) + " + btn,       lambda: (inj.down(["down"] + f), wait(0.033), inj.up(["down"] + f), wait(0.03), inj.down(["down"] + f), wait(0.017), inj.down([key]), wait(0.05), inj.up([key, "down"] + f))),
        ("22 (down twice) + " + btn,           lambda: (inj.down(["down"]), wait(0.033), inj.up(["down"]), wait(0.03), inj.down(["down"]), wait(0.017), inj.down([key]), wait(0.05), inj.up([key, "down"]))),
        ("44 (back dash) + " + btn,            lambda: (inj.down(b), wait(0.03), inj.up(b), wait(0.03), inj.down(b), wait(0.05), inj.down([key]), wait(0.05), inj.up([key] + b))),
        ("41236 + " + btn,                     lambda: (inj.down(b), wait(0.033), inj.up(b), inj.down(["down"] + b), wait(0.033), inj.up(b), wait(0.017), inj.down(["down"] + f), wait(0.033), inj.up(["down"]), wait(0.017), inj.down([key]), wait(0.05), inj.up([key] + f))),
    ]
    print(f"probing for cmd {want_cmd} with {btn}. F10 aborts.")
    for name, do in recipes:
        if "F10" in hot.pressed():
            break
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 3.0:
            me.refresh()
            if neutral(me):
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
        if mark and not saved:
            # a recipe whose name maps to a token can be written straight into
            # the character's table, so the replay never has to guess again
            digits = RECIPE_TOKENS.get(name.split(" ")[0])
            if digits:
                i = log["cmds"].index(want_cmd)
                ids = [m for m in log["ids"]][max(0, i - 1):]
                tok = f"{digits}{btn}"
                tab = read_commands()
                ch = me.get("CurrentCharacter")
                sec = (tab.setdefault("chars", {}).setdefault(str(ch), {})
                       if ch is not None else tab)
                sec[tok] = {"cmd": int(want_cmd), "move": ids[0] if ids else None,
                            "ids": ids}
                with open("commands.json", "w", encoding="utf-8") as fh:
                    json.dump(tab, fh, indent=1)
                print(f"      saved {tok} = cmd {want_cmd} for character {ch} "
                      f"(commands.json)")
                saved = True
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
    char_loaded = [None]
    layout = load_layout()
    pid = find_pid(args.process or layout["process"])
    if pid is None:
        print("game not running"); sys.exit(1)
    proc = Process(pid)
    anchors = locate_all(proc, layout)
    if any(v is None for v in anchors.values()):
        print(f"anchors did not resolve: {anchors}"); sys.exit(2)
    try:
        with open("pos.json", encoding="utf-8") as fh:
            pos_off = json.load(fh)            # {"me": off, "foe": off} inside the object
    except (OSError, ValueError):
        pos_off = {}

    def rebind():
        a = locate_all(proc, layout)
        if any(v is None for v in a.values()):
            return None
        sides = {"P1": Side(proc, layout, a, "P1"), "P2": Side(proc, layout, a, "P2")}
        if pos_off:
            sides["P1"].pos = sides["P1"].base + int(pos_off.get("me", 0xC0))
            sides["P2"].pos = sides["P2"].base + int(pos_off.get("foe", 0xC0))
        return sides

    sides = rebind()
    if sides:
        # the codes a character actually produces are in that character's
        # own section: load it before the first recording
        try:
            sides[args.me].refresh()
            ch = sides[args.me].get("CurrentCharacter")
            if ch is not None and 0 <= ch < 128:
                char_loaded[0] = ch
                load_commands(char=ch)
        except Exception:
            pass
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
        sides = rebind() or sides
        me = sides["P1"] if me is None or me.base == sides["P1"].base else sides["P2"]
        steps = plan(events)
        tries = {}
        print("\nrecorded: " + "  ".join(f"{s['tok']}->{s['mv']}@{s['prev_fr']}" for s in steps))
        menu = ("F8 replay   F9 keep retrying until it matches   "
                "F5 record again   F7 save   F10 quit")
        print(menu)
        while True:
            keys = hot.pressed()
            if "F10" in keys:
                inj.release_all(); return
            if "F5" in keys:
                break
            if "F9" in keys:
                # Each replay advances one candidate per unknown code, so a
                # stage with two unknowns needs a dozen presses of F8. Do them.
                foe_s = [o for o in sides.values() if o is not me][0]
                for round_n in range(1, 41):
                    print(f"  --- auto try {round_n} ---")
                    got, want = replay(me, steps, inj, facing_right,
                                       args.lag_frames, tries, foe=foe_s)
                    if got == want:
                        print(f"  solved in {round_n} tries")
                        break
                    if "F10" in hot.pressed():
                        print("  stopped")
                        break
                    time.sleep(0.4)
                else:
                    print("  gave up after 40 tries - the code may need a "
                          "stance or a state the replay cannot reach")
                print(menu)
            if "F7" in keys:
                with open("demo.json", "w", encoding="utf-8") as fh:
                    json.dump({"events": events, "steps": steps}, fh, indent=1)
                print("  saved demo.json")
            if "F8" in keys:
                replay(me, steps, inj, facing_right, args.lag_frames, tries,
                       foe=[o for o in sides.values() if o is not me][0])
                print(menu)
            time.sleep(0.01)


if __name__ == "__main__":
    main()
