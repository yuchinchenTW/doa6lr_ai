"""
holdbot.py - DOA6 Last Round auto-hold: read the opponent's strike type, pick
the hold that beats it, input it inside the startup window.

    python holdbot.py --probe      watch the detection only, no input at all
    python holdbot.py --dry-run    decide and log, inject nothing
    python holdbot.py              run for real

Offline, training mode and local versus only.

Same shape as the DOA5 bot, with the differences DOA6LR's memory forces:

  * Addresses come from layout.json via fields.py (two static pointer chains
    rooted in DOA6LR.exe), not from an AOB anchor.
  * The game exposes a PHASE field (0 startup, 1 active, 2 recovery) but no
    "total startup" - so the bot learns startup per (character, move id) by
    watching the phase flip, and stores it in startup.json. An unseen move is
    blocked, not held; the second time it comes the bot knows the window.
  * Facing is derived from the two X coordinates, since no direction field
    has been identified yet. Back-turned states are not detected.
  * MoveType (+0x578) only says "performing a command"; MoveKind (+0x108)
    says which: 3 strike, 16 throw, 5 hold, 0 nothing - the same values
    DOA5's MoveType used. A throw startup is answered with a jab.

Trigger:

    foe.MoveKind in (2, 3) and foe.Phase == 0   a strike, still in startup
    and foe.StrikeType in 0..5                  and the game says what kind
    and distance < --distance
    and min_remaining <= startup - CurrentMoveFrame <= window

The DOA5 lesson still applies: StrikeType and HighMidLow LATCH the last
attack while idle, so MoveType/Phase must gate them.
"""

import argparse
import ctypes
import json
import os
import struct
import sys
import time

from fields import STRIKE_TYPE, field_address, load_layout, locate_all, read_field
from memlib import Process, find_pid
from pad import (HOLD_DIRECTIONS, STRIKE_TO_HOLD, dirs_to_names,
                 disable_high_res_timer, enable_high_res_timer, guard, hold,
                 make_injector, parse_hold_overrides, strike as jab)

HERE = os.path.dirname(os.path.abspath(__file__))
STARTUP_FILE = os.path.join(HERE, "startup.json")
EXCEPTIONS_FILE = os.path.join(HERE, "exceptions.json")

MT_IDLE, MT_STRIKE, MT_HIT, MT_THROWN = 2, 1, 3, 5
# MoveKind (+0x108) is the DOA5 MoveType equivalent and the real gate:
# 3 strike, 16 throw startup, 5 hold, 0 idle / guard / movement.
MK_STRIKES, MK_THROW, MK_HOLD = (2, 3), 16, 5
PH_STARTUP, PH_ACTIVE, PH_RECOVERY = 0, 1, 2
# Move ids of hit-reaction animations seen so far: 24003..25782. Being in one
# means an input will not come out, whatever MoveType says.
HIT_REACTION = (24000, 27000)
LUNGE_PRECURSORS = (188,)        # run-up ids that end in the 8149 lunge
LUNGE_THROWS = (8149,)           # lunge throws that grab a croucher
DUCK_THROWS = ()                 # empty: "down" gives id 31 and 8144/8148/8284/
                                 # 8340 all grabbed us there (two matches, 0 whiffs)
BACK_THROWS = (8144, 8148, 8284) # runners: walk/dash away (8144 x3, 8143 x3 whiffed)
CMD_THROW_T = 363                # CommandCode of the plain T throw (move 8142):
                                 # the only kind the manual says can be escaped
LOW_THROW_CMDS = ()              # the direction guess (373 = 1T) was WRONG: 8144
                                 # reads 373 and grabbed us standing 3 times.
                                 # Seen: 8142 T=363, 8340=364, 8149=369, 8144=373,
                                 # 8148=1349. Only HighMidLowGround 3 means low
THROWS_FILE = "throws.json"      # {char: {move: {"cmd": {code: n}, "hml": {v: n}}}}
ANSWERS_FILE = "throw_answers.json"  # {char: {move: {"duck": [ok, n], "back": [..], "side": [..]}}}
ESCAPES_FILE = "throw_escapes.json"  # {"cmd<CommandCode>": {"T": [ok, n], "6T": [..], "4T": [..], "2T": [..]}}
TDMG_FILE = "throw_damage.json"  # {char: {move: {"dmg": what it has cost us, "hi": times it
                                 #                 grabbed us out of our own attack}}}
# ESCAPES_FILE is keyed by the throw's CommandCode, not (character, move): the break input
# is the throw's own command, so every character's 6T is broken the same
# way, and survival mode shows each character's throws only once or twice
# a throw is broken by matching its command: plain T for a neutral throw
# (cmd 363, 4/4 with T), direction + T for a command throw. First guess by
# the CommandCode seen at the start, then learn per throw like the answers.
# calibration (comboreplay --calibrate, Nyotengu): our own 6T reads cmd 364,
# 4T reads 1349, 2T reads 366 - so 365 is NOT 4T (left to the scoreboard)
ESCAPE_GUESS = {363: "T", 400: "T",          # both broke with T every time (4/4, 2/2, 1/1 x3)
                364: "6T", 1349: "4T", 366: "2T", 367: "2T"}
ESCAPE_OPTIONS = ("T", "6T", "4T", "2T", "3T", "1T")   # 3T/1T: diagonal throws (cmd 365 sits
                                                        # between 6T=364 and 2T=366; T 15/47)
NOHOLD_FILE = "nohold.json"      # {char: [move ids]} - strikes our hold "caught" for 0 dmg
STUN_FILE = "stun_holds.json"    # {our reaction id: [landed, tried]} - holds attempted
                                 # while that stun animation was playing
OH_FILE = "oh.json"              # {char: [move ids]} - strikes that are really
                                 # offensive holds: our hold got THROWN by them

# Per-character offence. Character 21 = Nyotengu (女天狗). Source: the DOA6 Last
# Round wiki (w.atwiki.jp/deadoralive6/pages/78, updated 2025-03-08), 中量 recipes,
# and the official move list (ML_nyotengu_v106.pdf):
#   P+K 目潰し  10 f high, 20 dmg, normal hit = long critical stun, launcher is
#               frame-guaranteed ("近距離戦の9割")
#   3P  鬼太刀  16 f mid launcher (PPP is the same move: PP gives +29, so P,P,P)
#   8K / H+K    the other launchers (18 f high / 19 f mid)
#   air:  3P/8K start, 中量: SSS -> 飛天2K -> 2T ; H+K start: 飛天PP -> 飛天PPP -> 2T
#   S,S,S is the Fatal Rush in the air; P+K after a hit enters 飛天の舞い (Hiten);
#   Hiten 2K (飛天下駄時雨) bounds; 2T on the downed foe is the ground throw.
#   After the mid-P hold "受け身を取らない相手には2Tが確定": ground throw too.
# Keys: opener move id -> string; "default" for any other opener; "ground" is
# pressed when the foe lies down after one of our holds.
# What the log taught us about the keyboard: a diagonal + button never came
# out as the command (3P all at once = 6P; 3 a frame early = crouch id 8 and
# the P dropped), and the CPU HELD the 3P/PPP launcher out of the stun 6 of 7
# times (8215: 41% of our damage taken). The Fatal Rush is the answer to both:
# S alone, no direction, and once the first S lands on a critical stun the
# rest is a Fatal Stun that cannot be held; the 4th S becomes the Break Blow
# by itself when the gauge is full - the gauge we could never read.
CHAR_COMBOS = {
    21: {"default": "S,S,S,S",           # after P+K (or any single stun hit)
         176: "P,S,S,S,S"},              # after a plain P: PP is the +29 stun
    31: {"default": "P,P,K"},            # Kula: fallback when the pool is off
    35: {"default": "P,P,P,P"},          # Minato: the guide's universal juggle
    # "ground": "2T" after a hold is off: the CPU techs every time (2T came
    # out as 8422/8143, 0 dmg x5) and the whiff left us busy for its next hit
}
# Candidate strings per opener, tried in turn and scored by damage dealt
# (combo_stats.json, per character). Only buttons and horizontals - the
# keyboard cannot do diagonals - and only strings whose later hits are
# branches (those buffer; a fresh move is taken only once we are idle).
#   PP6PK 鞍馬乱舞 (high high high low), PPK 夜叉神楽 (mid kick 25),
#   PP2KK 跳ね神楽 (low, mid 28), 6PK 鬼神楽 (mid mid), and the Fatal Rush.
RECIPE_POOL = {
    21: {"default": ["S,S,S,S", "P,P,6P,K", "P,P,K", "P,P,2K,K", "6P,K", "S", "P"],
         176:       ["P,S,S,S,S", "P,6P,K", "P,K", "P,2K,K", "P", "S"],
         # after the 2K that made a fast throw whiff: a short stun, the
         # launcher string never came out of it (Kula: 8K,6P,P 0 for ~60)
         "lowkick":  ["P", "6P,K", "S"]},
    # Mai: PPPP (mid mid, +16, launches), PPK (mid kick 22, +11), KKK (high x3,
    # +22), 6PKK (mid, low, mid 18 +23), PP2K (low 25+12), plus the Fatal Rush
    # and the one-hit options. All buttons + horizontals, all string branches.
    30: {"default": ["S,S,S,S", "P,P,P,P", "P,P,K", "K,K,K", "6P,K,K", "P,P,2K", "S", "P"],
         176:       ["P,P,P", "P,K", "P,2K", "S,S,S,S", "P", "S"],
         "lowkick":  ["P", "6P,K,K", "S"]},
    # Kula: 8PP (+19 stun), PPK (safe pressure), 6PP (+23 stun on CH), KKK
    # (bound), PP6P; 8K launches on a normal hit (15 f) with 9PK / 6PP as
    # the guide's juggles; 9KP is a +34 stun; 236P Diamond Breath freezes.
    # 9K / 9P are diagonals and 236P a motion - the calibration showed
    # diagonals register when the character has the move, so they are in
    # the pool and the net-damage bandit decides.
    31: {"default": ["8P,P", "P,P,K", "6P,P", "K,K,K", "P,P,6P", "8K,6P,P", "8K,9P,K",
                     "9K,P", "236P,6P,P", "S,S,S,S", "S", "P"],
         176:       ["P,K", "P,6P", "P,P,K", "S,S,S,S", "P", "S"],
         "lowkick":  ["P", "6P,P", "S"]},
    # Minato: launchers 8K (18 f), 8P (15 f, crouch-dash upper), 2P+K (18 f)
    # and 3P+K; the guide's juggle is PPPP off 8K / 8P / 3P+K and 6KK off
    # 2P+K, with 6PPP as the bound ender. Fast strings: PKK 10 f, 6PP 12 f
    # (the 2nd hit tracks), KP 12 f, 4PK 14 f, 3KK 15 f. 66P is the
    # long-reach +21 tool. "2PK" is P+K with a direction: the token parser
    # takes the leading numpad digits and PK is the P+K button.
    # Her calibration (commands.json, chars/35) settles which inputs are real:
    # 8P 190, 8K 191, 2P+K 8108, 6P 177, K 179, 4P 8071, 6K 8087 all distinct,
    # and 236P 8130 (cmd 5900) is the Combo Challenge stage-1 move and her
    # only motion input: 46P/46K/46PK/64PK/214P all fall back to their last
    # direction + button. 3K and 3P
    # collapse to 6K / 6P and 1P/7P to 4P, so the diagonals are not worth a
    # pool slot - 3KK is dropped for 6KK.
    # 3P+K (move 8109) was added once the down-diagonal recipe made it
    # reachable: the guide lists her launchers as 8K / 9P / 8P / 3P+K / 2P+K
    # and the juggle off 8K, 8P and 3P+K as PPPP. Until 2026-09-18 a 3P+K
    # input came out as 6P+K, so the guide's own combo was impossible.
    35: {"default": ["8K,P,P,P,P", "8P,P,P,P,P", "3PK,P,P,P,P", "2PK,6K,K", "6P,P,P",
                     "P,K,K", "K,P", "4P,K", "6K,K", "66P", "236P",
                     "S,S,S,S", "S", "P"],
         176:       ["P,K,K", "P,P,P,P", "6P,P", "S,S,S,S", "P", "S"],
         "lowkick":  ["P", "6P,P", "S"]},
}
# any other character we play: strings every DOA6 character has, scored the
# same way (P string, PPK, the Fatal Rush, and the two one-hit "take it and
# leave" options that a hold-happy CPU makes attractive)
GENERIC_POOL = {"default": ["P,P,K", "P,P,P", "S,S,S,S", "S", "P"]}
COMBO_STATS_FILE = "combo_stats.json"   # {char: {opener: {recipe: [n, dmg]}}}
# Mai Shiranui (不知火舞, char 30 - read off the banner of a match the user
# played as her). Free Step Dodge frame
# data + the FSD beginners' guide + doa6.seesaa.net: P 9 f is the fastest jab
# in the game but only -2 on hit; 4P (15 f, high) gives a +35 stun on a
# NORMAL hit, which is what our stun follow-ups want. Launchers she has need
# diagonals (3P, 3P+K) or 8K (up+K = a sidestep here), so the pool is strings.
CHAR_POKE = {21: "pk",           # P+K is her poke; P (14 f, +1 on hit) is not
             30: "4P",           # Mai: back+P, +35 stun on normal hit
             # Kula Diamond (char 31, Free Step Dodge beginners' guide): P is
             # 9 f but -5 on hit, 6P 11 f is -11 on a normal hit. 8P (17 f,
             # rising mid punch) gives a +29 lift stun on a NORMAL hit and
             # needs only up + P, which the keyboard sends reliably.
             # ...but 8P as a poke came out as a sidestep 3 of 7 times in a
             # match (up alone = free step) and landed 1/7. 6P (11 f, mid,
             # +20 stun on counter hit) is the reliable horizontal.
             31: "6P",
             # Minato (char 35, the DLC released 2026-09-10; doa6wiki and the
             # goziline guide - Free Step Dodge has no frame data for her yet).
             # 6K is 13 f and +17 on a NORMAL hit, and the guide says her 8P
             # launcher is guaranteed after it: a poke that opens straight
             # into the juggle. 66P is better on paper (+21, -4 on block, long
             # reach) but it is a dash that closes the gap into throw range,
             # which is where this bot loses rounds, so it sits in the pool
             # instead and the bandit prices it.
             35: "6K"}
GENERIC_COMBO = "P,P,P,K"
CHAR_NAMES = {21: "Nyotengu", 30: "Mai", 31: "Kula", 35: "Minato"}
CHAR_PUNCH_REACH = {31: 105}     # how far the standing P punish still lands
# startup of the standing P, for the "interrupt a slow throw with a punch"
# answer: it has to be live before the grab. Nyotengu's 14 f P lost the
# race against char 7's 16-frame 8109 (hi-counter grab, unbreakable) with
# the flat 11-frame threshold that Kula's 9 f P made look fine (5/6).
# Mai's value is a guess from the guest characters' usual 10-11 f jab.
CHAR_P_STARTUP = {21: 14, 30: 11, 31: 9, 35: 10}   # Minato's PKK is 10 f


def throw_class(cmd, hml):
    """'low' grabs only crouchers (we stand: it whiffs by itself), 'T' is the
    escapable standard throw, 'dir' everything else (running/directional)."""
    if hml == 3 or cmd in LOW_THROW_CMDS:
        return "low"
    if cmd == CMD_THROW_T:
        return "T"
    return "dir"
GUARD_MOVES = (270, 271)     # stand / crouch guard animation ids
# Left-over crouch states after a crouch guard (271 -> 10 -> 13, then 127 /
# 131 while it settles). A hold input from them came out as a 30-frame
# step (ids 80-91) in every one of eight tries, so wait them out.
CROUCH_MOVES = (10, 13) + tuple(range(125, 136))
DUCK_IDS = (8, 9, 10, 13, 271)   # what down+back produces: 8/9 first, then 13.
                                 # Down alone is a SIDESTEP (31/32) in DOA6
HOLD_IDS = (152, 153, 154, 155, 156)   # our hold animations (whiff)
WALK_FWD, WALK_BACK = 1, 2   # generic walk ids; the facing oracle
FWD_IDS, BACK_IDS = (1, 3), (2, 4)   # walk / dash forward, walk / dash back
MT_HOLD_HIT = 6              # our hold has caught the attack (DOA5 used 6 too)

_user32 = ctypes.WinDLL("user32")
_VK = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
       "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D}


def keys_physically_down():
    """Bound keys the OS currently reports as down, BEFORE we inject.

    A hold is direction + H together. If H (or a direction) is already held
    by a human hand or a drifting pad, our input arrives as a mere direction
    change and the game answers with a crouch (13) or a step (79/83/87/91)
    instead of a hold - which is exactly what two whole sessions produced.
    """
    return [k for k, vk in _VK.items()
            if _user32.GetAsyncKeyState(vk) & 0x8000]


# Read the whole head of the state object in one call per tick, then decode
# fields from the buffer. 0x1000 bytes covers every offset in layout.json.
BLOCK = 0x1000


class Side:
    """Fast reader for one player's fields out of a single block read."""

    def __init__(self, proc, layout, anchors, side):
        self.proc = proc
        self.base = (anchors.get(f"state:{side}", anchors["state"])
                     + layout["players"][side]["state"])
        # the XAxis field's own offset (+0x5B0) on top of the side's slot
        self.pos = (anchors["pos"] + layout["players"][side]["pos"]
                    + layout["fields"]["XAxis"][0])
        self.f = {}
        for name, (off, kind, an) in layout["fields"].items():
            if an == "state":
                self.f[name] = (off, kind)
        self.buf = b""

    def refresh(self):
        self.buf = self.proc.read_tolerant(self.base, BLOCK)
        return self

    def get(self, name):
        off, kind = self.f[name]
        fmt = {"u8": "<B", "u16": "<H", "u32": "<I", "i32": "<i",
               "f32": "<f", "u64": "<Q", "f64": "<d"}[kind]
        return struct.unpack_from(fmt, self.buf, off)[0]

    def xyz(self):
        b = self.proc.read_tolerant(self.pos, 12)
        return struct.unpack("<3f", b)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class StartupTable:
    """(character, move) -> startup frames, learned from phase flips.

    The frame counter is 1 on the first frame of the move; the value it has
    on the first ACTIVE frame is what DOA5 called TotalStartup + 1. We store
    the counter value at the flip, so remaining = stored - CurrentMoveFrame
    is frames until the attack is live. Polling can land up to a frame
    late, so the minimum over observations is kept.
    """

    def __init__(self, path=STARTUP_FILE):
        self.path = path
        raw = load_json(path, {})
        self.table = {(int(c), int(m)): int(v) for c, moves in raw.items()
                      if not c.startswith("_") for m, v in moves.items()}
        # (character, move) -> how often it was seen in startup without a
        # startup ever being learned. Persisted: a run-up stays a run-up.
        self.seen = {(int(c), int(m)): int(v) for c, moves in
                     raw.get("_seen", {}).items() for m, v in moves.items()}
        self.dirty = False

    def get(self, char, move):
        return self.table.get((char, move))

    def learn(self, char, move, frame):
        key = (char, move)
        old = self.table.get(key)
        if old is None or frame < old:
            self.table[key] = frame
            self.dirty = True
            return True
        return False

    def note_unknown(self, char, move):
        k = (char, move)
        self.seen[k] = self.seen.get(k, 0) + 1
        self.dirty = True
        return self.seen[k]

    def save(self):
        if not self.dirty:
            return
        out = {}
        for (c, m), v in sorted(self.table.items()):
            out.setdefault(str(c), {})[str(m)] = v
        for (c, m), v in sorted(self.seen.items()):
            if (c, m) not in self.table:
                out.setdefault("_seen", {}).setdefault(str(c), {})[str(m)] = v
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        self.dirty = False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--process", default=None)
    ap.add_argument("--me", default="auto", choices=["P1", "P2", "auto"],
                    help="which side the keyboard controls. auto: assume P1, "
                         "then confirm with the facing probe - the character "
                         "that walks when we tap right is ours (a match was "
                         "played inverted once: we sat on P2)")
    ap.add_argument("--injector", default="keyboard",
                    choices=["keyboard", "vgamepad", "null"])
    ap.add_argument("--distance", type=float, default=250.0,
                    help="max distance in game units (~cm) to react; the "
                         "characters start ~190 apart in training")
    ap.add_argument("--window", type=int, default=16,
                    help="hold when this many or fewer frames remain. First "
                         "live run: remaining=18 landed 0/2, 16 landed 2/3, "
                         "15 landed 1/1 - the hold's own catch window opens "
                         "~4 frames after the input, so firing too early "
                         "lets it expire before the strike arrives")
    ap.add_argument("--min-remaining", type=int, default=6,
                    help="do not hold with fewer startup frames left: with "
                         "~50 ms of probe + input latency the hold arrives "
                         "after the hit (seen as our id 16000/16010)")
    ap.add_argument("--cooldown", type=float, default=0.25)
    ap.add_argument("--pretap", type=float, default=0.0,
                    help="seconds to tap the opposite direction before a "
                         "hold (DOA5's trick). Off: on DOA6 the tap puts us "
                         "in a walk state and the hold that follows tends to "
                         "come out as a guard")
    ap.add_argument("--press", type=float, default=0.020)
    ap.add_argument("--dir-lead", type=float, default=0.0)
    ap.add_argument("--hold-mode", default="3way", choices=["3way", "4way"],
                    help="the game's Versus/Free Training 'Holds' setting. "
                         "3-way: 4H covers both mids and 6H is just a guard "
                         "(move 270). 4-way: 4H mid punch, 6H mid kick, as "
                         "in DOA5. Must match the in-game setting")
    ap.add_argument("--hold", default="",
                    help="re-specify holds in numpad notation, e.g. midk=6; "
                         "applied after --hold-mode")
    ap.add_argument("--guard-types", default="",
                    help="StrikeType numbers to block instead of hold")
    ap.add_argument("--unknown", default="guard", choices=["guard", "skip",
                                                            "hold"],
                    help="what to do against a move whose startup is not "
                         "learned yet. 'hold' fires when CurrentMoveFrame "
                         "reaches --unknown-frame")
    ap.add_argument("--unknown-frame", type=int, default=6)
    ap.add_argument("--no-hold-in-stun", dest="hold_in_stun", action="store_false",
                    help="never attempt holds while in hit stun. Default: hold "
                         "there too (type-3 stuns with a 24xxx-26xxx id, after "
                         "--stun-hold-frame); a stun id where 3 tries all went "
                         "unregistered is retired to stun_holds.json")
    ap.add_argument("--stun-hold-frame", type=int, default=4,
                    help="in stun, only hold once our reaction animation has "
                         "run this many frames")
    ap.add_argument("--throw-answer", default="crouch",
                    choices=["crouch", "jab", "none"],
                    help="what to do when a throw starts. A throw is active at "
                         "frame 7 and a jab at frame 15, so jabbing on "
                         "reaction lost 20 of 34. Standing throws whiff on a "
                         "crouching character: duck, then punish the whiff")
    ap.add_argument("--crouch-time", type=float, default=0.25,
                    help="seconds to stay crouched against a far lunge throw")
    ap.add_argument("--duck-range", type=float, default=240.0,
                    help="a throw that starts farther than this is a lunge: "
                         "crouch (standing throws grab nothing on a croucher) "
                         "instead of backing off, which cannot outrun it")
    ap.add_argument("--throw-punish-range", type=float, default=300.0,
                    help="only answer an incoming throw inside this distance "
                         "(the CPU's dash throws start from 150+ away)")
    ap.add_argument("--jab-reach", type=float, default=130.0,
                    help="a punch only connects inside this distance; beyond "
                         "it a running throw is met by retreating instead")
    ap.add_argument("--throw-jab-frames", type=int, default=11,
                    help="if a throw's learned startup leaves at least this "
                         "many frames, interrupt it with a punch (P is live "
                         "at frame ~15); otherwise sidestep")
    ap.add_argument("--approach-attack", default="punch",
                    choices=["punch", "kick", "none"],
                    help="what to do when the foe is in a kind-3 move that "
                         "has never gone active in 2+ sightings (209/210/8015: "
                         "a run-up into a throw). Default: interrupt with a "
                         "punch inside --approach-range")
    ap.add_argument("--gauge-max", type=int, default=200,
                    help="BreakGauge value that shows as a full bar. The bars "
                         "were full on screen at 135 (ours) and 172 (theirs), "
                         "so the field keeps counting past full; 100 fits the "
                         "half-damage fill rate. Break Hold at half of this")
    ap.add_argument("--break-hold", action="store_true",
                    help="use 4S (Break Hold, any height) when the gauge proxy "
                         "says we can. OFF by default: +0x584 resets every "
                         "round while the real gauge carries over, so the "
                         "estimate is wrong and a refused 4S is a whiffed "
                         "Fatal Rush (6 of 12 in one match, three lost rounds). "
                         "Plain holds land 75-100%%")
    ap.add_argument("--no-break-blow", dest="break_blow", action="store_false",
                    help="do not spend a full Break Gauge on 6S (Break Blow): as "
                         "the punish after a guard, and as the combo on a stunned "
                         "foe. On by default now that the gauge is read for real")
    ap.add_argument("--poke", default="auto",
                    choices=["kick", "punch", "pk", "hk", "4P", "6P", "8P", "auto", "none"],
                    help="attack to throw out when the foe is idle or walking "
                         "in at --poke-min..--poke-max, or is getting up close "
                         "by. Strikes beat throws: the 7-frame dash throw "
                         "(8340) cannot be reacted to, only pre-empted")
    ap.add_argument("--close-throw-range", type=float, default=75.0,
                    help="inside a fast unanswerable throw's reach, grab an idle "
                         "opponent this close with our own T before it grabs us - "
                         "29/47 against char 2's 7-frame dash throw, 0/14 against "
                         "characters without one, so only there; scored per opponent "
                         "in close_throw.json (0 disables)")
    ap.add_argument("--poke-min", type=float, default=60.0)
    ap.add_argument("--poke-max", type=float, default=190.0)
    ap.add_argument("--combo", default="auto",
                    help="string to continue with once one of our strikes "
                         "connects (foe in hit stun, kind 8) or they are "
                         "airborne (kind 9). Each next button is buffered "
                         "while our current hit is still out, so the game "
                         "chains it as a string instead of separate jabs. "
                         "Tokens: P K T S PK HK, optional numpad direction "
                         "(6P, 3K, 2K); comma separated. 'none' disables")
    ap.add_argument("--combo-range", type=float, default=110.0,
                    help="only juggle an airborne foe (kind 9) from within this")
    ap.add_argument("--poke-gap", type=float, default=0.45,
                    help="seconds between pokes")
    ap.add_argument("--zone", type=float, default=180.0,
                    help="when both sides are idle closer than this, walk "
                         "back out to --zone-out. Standing idle inside throw "
                         "range is where nearly all the damage came from: a "
                         "5-frame throw cannot be reacted to, but a runner "
                         "from 190+ can. 0 disables")
    ap.add_argument("--zone-out", type=float, default=220.0)
    ap.add_argument("--approach-range", type=float, default=200.0,
                    help="react to a run-up inside this distance: punch "
                         "within --jab-reach, back off beyond it")
    ap.add_argument("--punish-after-guard", default="auto",
                    choices=["none", "punch", "throw", "auto"],
                    help="hit back the moment a blocked attack is in recovery. "
                         "auto: throw when close, punch otherwise")
    ap.add_argument("--punish-delay", type=float, default=0.10,
                    help="seconds after the guard drops before punishing "
                         "(block stun eats earlier inputs)")
    ap.add_argument("--punish-throw-range", type=float, default=95.0)
    ap.add_argument("--punish-punch-range", type=float, default=170.0)
    ap.add_argument("--punish-min-recovery", type=int, default=12,
                    help="only punish when the blocked move still has at "
                         "least this many animation frames left")
    ap.add_argument("--no-punish-throws", dest="punish_throws",
                    action="store_false",
                    help="do not jab at an incoming throw (MoveKind 16)")
    ap.add_argument("--poll-hz", type=float, default=500)
    ap.add_argument("--no-trace", dest="trace", action="store_false",
                    help="do not print the per-frame trace of holds that "
                         "failed without producing a hold animation")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--test-combo", default=None, metavar="STRING",
                    help="Training-mode check: press this comma string (or "
                         "'all' for every one in the character's pool and in "
                         "combo_challenge.json) through the same input code a "
                         "match uses, and print the move id each token "
                         "produced against what the calibration expects. "
                         "Nothing else runs.")
    ap.add_argument("--test-repeat", type=int, default=1,
                    help="how many times to run each string in --test-combo")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    layout = load_layout()
    if layout is None:
        print("no layout.json; run autoscan/probe first.")
        sys.exit(1)
    name = args.process or layout["process"]
    pid = find_pid(name)
    if pid is None:
        print(f"{name} is not running.")
        sys.exit(1)
    proc = Process(pid)
    anchors = locate_all(proc, layout)
    if any(v is None for v in anchors.values()):
        print(f"anchors did not resolve: {anchors}. Not in a match, or the "
              f"layout is stale (re-run autoscan/pointerscan).")
        sys.exit(2)
    side_confirmed = [args.me != "auto"]
    side_votes = [0]
    if args.me == "auto":
        args.me = "P1"
    me_side, foe_side = args.me, ("P2" if args.me == "P1" else "P1")
    me, foe = Side(proc, layout, anchors, me_side), Side(proc, layout, anchors, foe_side)

    def find_p2_pos_row():
        """Find P2's coordinate row in the position block by BEHAVIOUR, not
        by a fixed offset. P1's row is +0x5B0. The block also holds P1's
        other rows (bones/targets a few units away) and P2's rows some
        pairing-dependent stride later (+0x60, +0x120 ... seen). A pattern
        match picked P1's own +0x5D0 row once (stride 0x20: distance read 0
        all match, 0-3). So: sample the block for a quarter second and keep
        only vec4 rows (w = 1) that stay within 1500 of P1 but are NEVER
        within 12 of it - P1's own bones ride along with it, the opponent
        does not. Prefer a row followed by a scalar row like P1's."""
        base = anchors["pos"]
        samples = []
        for _ in range(6):
            samples.append(proc.read_tolerant(base + 0x500, 0x600))
            time.sleep(0.04)
        def row(blk, off):
            return struct.unpack_from("<4f", blk, off - 0x500)
        def is_vec(r):
            return r[3] == 1.0 and all(abs(v) < 1e6 for v in r[:3])
        def is_scalar(r):
            return r[0] > 0 and r[1] == 0 and r[2] == 0 and r[3] == 0
        p1s = [row(blk, 0x5B0) for blk in samples]
        if not all(is_vec(p) for p in p1s):
            return None                        # P1's own row is not live yet
        best = None
        for k in range(0x20, 0x500, 0x10):
            ok, mind = True, 1e9
            for blk, p1 in zip(samples, p1s):
                try:
                    r = row(blk, 0x5B0 + k)
                except struct.error:
                    ok = False
                    break
                if not is_vec(r) or abs(r[0] - p1[0]) > 1500 \
                        or abs(r[2] - p1[2]) > 1500 or abs(r[1] - p1[1]) > 400:
                    ok = False
                    break
                mind = min(mind, ((r[0] - p1[0]) ** 2 + (r[2] - p1[2]) ** 2) ** 0.5)
            if not ok or mind < 12:
                continue                       # P1's own bone, or garbage
            scalar_after = is_scalar(row(samples[-1], 0x5C0 + k))
            score = (0 if scalar_after else 1, k)
            if best is None or score < best[0]:
                best = (score, k)
        return best[1] if best else None

    STATE_SCAN = 0x120000       # each character object is ~1.1 MB

    def find_state_pos_offset():
        """Pairing-independent route: P1's coordinates (pos block +0x5B0,
        always right for us) must also live somewhere inside P1's own 1.1 MB
        state object. Find that offset, then read P2's coordinates at the
        same offset of P2's object. The shared pos block moved P2's row
        around with the pairing (+0x60, +0x120, and for char 34 nowhere in
        sight - a stale row 341 away all match)."""
        p1_addr = anchors["pos"] + layout["fields"]["XAxis"][0]
        try:
            x, y, z = struct.unpack("<3f", proc.read_tolerant(p1_addr, 12))
        except Exception:
            return None
        if not all(abs(v) < 1e6 for v in (x, y, z)) or (x, y, z) == (0.0, 0.0, 0.0):
            return None
        me_base = (me if me_side == "P1" else foe).base
        foe_base = (foe if me_side == "P1" else me).base
        try:
            data = proc.read_tolerant(me_base, STATE_SCAN)
        except Exception:
            return None
        key = struct.pack("<f", x)
        i = data.find(key)
        found = []
        while i != -1 and len(found) < 64:
            if i % 4 == 0 and i + 12 <= len(data):
                yy, zz = struct.unpack_from("<2f", data, i + 4)
                if abs(yy - y) < 0.5 and abs(zz - z) < 0.5:
                    found.append(i)
            i = data.find(key, i + 1)
        for off in found:
            try:
                fx, fy, fz = struct.unpack("<3f", proc.read_tolerant(foe_base + off, 12))
            except Exception:
                continue
            d = ((fx - x) ** 2 + (fz - z) ** 2) ** 0.5
            if all(abs(v) < 1e6 for v in (fx, fy, fz)) and 12 < d < 1500 \
                    and abs(fy - y) < 400:
                return off
        return None

    def find_p2_row_by_motion(window=0.8):
        """Last resort, and the only one that needs no layout knowledge:
        P2 is the coordinate row near P1 that moves INDEPENDENTLY of P1.
        Collect every vec4 (w = 1) within 3000 of P1 in the 2 MB around the
        position object, sample them for `window` seconds, and keep the rows
        that changed and whose displacement differs from P1's own (P1's
        bones ride along with P1; the opponent does not). Nothing moving
        (a pause, the KO freeze) -> None, and the caller retries later."""
        base = anchors["pos"]
        p1_addr = base + layout["fields"]["XAxis"][0]
        try:
            p1 = struct.unpack("<3f", proc.read_tolerant(p1_addr, 12))
        except Exception:
            return None
        cands = []
        for start in range(base - 0x100000, base + 0x100000, 0x10000):
            try:
                blk = proc.read_tolerant(start, 0x10000)
            except Exception:
                continue
            for off in range(0, len(blk) - 16, 0x10):
                x, y, z, w = struct.unpack_from("<4f", blk, off)
                if w == 1.0 and abs(x - p1[0]) < 3000 and abs(z - p1[2]) < 3000 \
                        and abs(y - p1[1]) < 1500 and (x, z) != (0.0, 0.0):
                    if ((x - p1[0]) ** 2 + (z - p1[2]) ** 2) ** 0.5 > 12:
                        cands.append(start + off)
        if not cands:
            return None
        t_end = time.perf_counter() + window
        p1_first = p1
        first = {a: struct.unpack("<3f", proc.read_tolerant(a, 12)) for a in cands}
        changed = set()
        while time.perf_counter() < t_end:
            for a in cands:
                if a in changed:
                    continue
                try:
                    v = struct.unpack("<3f", proc.read_tolerant(a, 12))
                except Exception:
                    continue
                if v != first[a]:
                    changed.add(a)
            time.sleep(0.05)
        p1_now = struct.unpack("<3f", proc.read_tolerant(p1_addr, 12))
        dp1 = (p1_now[0] - p1_first[0], p1_now[2] - p1_first[2])
        best = None
        for a in cands:
            try:
                v = struct.unpack("<3f", proc.read_tolerant(a, 12))
            except Exception:
                continue
            dv = (v[0] - first[a][0], v[2] - first[a][2])
            rel = ((dv[0] - dp1[0]) ** 2 + (dv[1] - dp1[1]) ** 2) ** 0.5
            moved_any = a in changed or abs(dp1[0]) + abs(dp1[1]) > 0
            if not moved_any or rel < 3:
                continue                       # static, or one of P1's own bones
            d = ((v[0] - p1_now[0]) ** 2 + (v[2] - p1_now[2]) ** 2) ** 0.5
            if d > 1500 or abs(v[1] - p1_now[1]) > 400:
                continue
            if best is None or d < best[0]:
                best = (d, a)
        return best[1] if best else None

    # the two fighters are never further apart than this in a real round
    # (the widest ever logged is ~400 at a round start); 778 came from a
    # bone array that had been saved into pos.json
    POS_MAX_D = 450.0

    def find_positions_in_objects(window=0.3):
        """Both coordinates from the two character objects, by motion. In the
        roommate's mode the shared position object was frozen for BOTH sides
        (our own row never moved while we were being thrown), so nothing
        anchored to it can work. Each character object carries its world
        transforms ~1 MB in (P1's at +0xFB280.. for char 21), but the offset
        differs per character, so: (1) in our object, find world-sized float
        triples that CHANGE over `window`; (2) in the foe's object, find a
        changing triple within 3000 of ours. Returns (my_addr, foe_addr)."""
        me_obj = (me if me_side == "P1" else foe).base
        foe_obj = (foe if me_side == "P1" else me).base
        try:
            a1 = proc.read_tolerant(me_obj, STATE_SCAN)
            b1 = proc.read_tolerant(foe_obj, STATE_SCAN)
            time.sleep(window)
            a2 = proc.read_tolerant(me_obj, STATE_SCAN)
            b2 = proc.read_tolerant(foe_obj, STATE_SCAN)
        except Exception:
            return None
        def world(x, y, z):
            return (100 < abs(x) < 3e5 and 100 < abs(z) < 3e5 and abs(y) < 5000
                    and x == x and y == y and z == z)
        def moving_triples(d1, d2):
            out = []
            for i in range(0, min(len(d1), len(d2)) - 16, 4):
                if d1[i:i + 12] == d2[i:i + 12]:
                    continue                   # unchanged: not a live position
                x, y, z = struct.unpack_from("<3f", d2, i)
                if not world(x, y, z):
                    continue
                x0, y0, z0 = struct.unpack_from("<3f", d1, i)
                if not world(x0, y0, z0) or abs(x - x0) > 300 or abs(z - z0) > 300:
                    continue                   # garbage flicker, not a walk
                w = struct.unpack_from("<f", d2, i + 12)[0]
                out.append((0 if w == 1.0 else 1, i, (x, y, z)))
            return out
        mine = moving_triples(a1, a2)
        theirs = moving_triples(b1, b2)
        if not mine or not theirs:
            return None
        mine.sort()
        pairs = []
        for mp, my_off, my_xyz in mine[:6]:
            for fp, off, (x, y, z) in theirs:
                d = ((x - my_xyz[0]) ** 2 + (z - my_xyz[2]) ** 2) ** 0.5
                if 12 < d < 3000 and abs(y - my_xyz[1]) < 1500:
                    pairs.append(((mp + fp, d), my_off, off))
        # the same offset in both objects is most likely the same field (the
        # root): both objects showed a live vec4 at +0xC0. Try those first.
        def rank(t):
            (_, d), my_off, foe_off = t
            mx, my_, mz = struct.unpack_from("<3f", a2, my_off)
            fx, fy, fz = struct.unpack_from("<3f", b2, foe_off)
            # same field in both objects first; both feet on the same floor
            # (small y gap) next; then the lowest offset
            return (0 if my_off == foe_off else 1, 0 if my_off < 0x1000 else 1,
                    round(abs(my_ - fy) / 25), my_off)
        pairs = [t for t in pairs if t[1] == t[2]]
        if not pairs:
            return None       # +0xC0/+0xD0 in both objects every good match; a
                              # mixed pair (+0xD0 / +0xD4740) was a bone array
                              # that happened to move while the row was still
        pairs.sort(key=rank)  # unfilled. Better no fix now than a wrong one
        # +0xC0/+0xD0 is the root transform and has held for every character
        # so far. A bone array ~1 MB in also moves and also passes "world
        # sized", and once it was written to pos.json a whole session read
        # distances of 300-780: no throw was ever answered (they are skipped
        # past 300) and the bot was thrown to death. If anything small is on
        # offer, nothing else is considered.
        if any(t[1] < 0x1000 for t in pairs):
            pairs = [t for t in pairs if t[1] < 0x1000]
        for _, my_off, foe_off in pairs[:8]:
            ok = True
            prev = None
            for _ in range(4):
                try:
                    m = struct.unpack("<3f", proc.read_tolerant(me_obj + my_off, 12))
                    f = struct.unpack("<3f", proc.read_tolerant(foe_obj + foe_off, 12))
                except Exception:
                    ok = False
                    break
                if not (world(*m) and world(*f)):
                    ok = False
                    break
                d = ((m[0] - f[0]) ** 2 + (m[2] - f[2]) ** 2) ** 0.5
                if d > POS_MAX_D or abs(m[1] - f[1]) > 600:
                    ok = False
                    break
                if prev is not None and (abs(m[0] - prev[0][0]) > 400 or abs(m[2] - prev[0][2]) > 400
                                         or abs(f[0] - prev[1][0]) > 400 or abs(f[2] - prev[1][2]) > 400):
                    ok = False               # teleporting: a timer or a matrix, not a body
                    break
                prev = (m, f)
                time.sleep(0.1)
            if ok:
                return me_obj + my_off, foe_obj + foe_off, my_off, foe_off
        return None

    POS_FILE = "pos.json"
    try:
        with open(POS_FILE, encoding="utf-8") as fh:
            _pm = json.load(fh)
        pos_mem = [int(_pm["me"]), int(_pm["foe"])]
    except (OSError, ValueError, KeyError, TypeError):
        pos_mem = [None, None]  # (our offset, their offset) once found by motion
    hold_dmg = {}               # hold kind -> [total foe hp lost, count]
    hold_watch = [None]         # {"kind", "hp0", "t"} while a caught hold plays out
    last_hold_caught = [0.0]

    def match_live():
        try:
            me.refresh(); foe.refresh()
            return me.get("CurrentHealth") > 0 and foe.get("CurrentHealth") > 0
        except Exception:
            return False

    def remembered_pair_ok():
        """Between fights the objects move; the field offsets do not. Try the
        offsets that worked before the search starts over."""
        if pos_mem[0] is None:
            return False
        my_a, foe_a = me.base + pos_mem[0], foe.base + pos_mem[1]
        try:
            for _ in range(3):
                m = struct.unpack("<3f", proc.read_tolerant(my_a, 12))
                f = struct.unpack("<3f", proc.read_tolerant(foe_a, 12))
                if not all(100 < abs(v) < 3e5 for v in (m[0], m[2], f[0], f[2])):
                    return False
                d = ((m[0] - f[0]) ** 2 + (m[2] - f[2]) ** 2) ** 0.5
                if not 5 < d < POS_MAX_D or abs(m[1] - f[1]) > 600:
                    return False
                time.sleep(0.05)
        except Exception:
            return False
        me.pos, foe.pos = my_a, foe_a
        print(f"position: remembered offsets still valid (ours +0x{pos_mem[0]:X}, "
              f"theirs +0x{pos_mem[1]:X}), distance now {d:.0f}")
        return True

    def fix_pos_rows(quiet=False):
        if not match_live():
            return False                   # between fights: objects reload, values are noise
        for _ in range(3):
            if remembered_pair_ok():
                return True
            time.sleep(0.15)
        got = find_positions_in_objects()
        if got is not None:
            my_addr, foe_addr, my_off, foe_off = got
            me.pos, foe.pos = my_addr, foe_addr
            keep = pos_mem[0] is not None and pos_mem[0] < 0x1000 and my_off >= 0x1000
            pos_mem[:] = [my_off, foe_off]
            if keep:
                print(f"  position: using +0x{my_off:X} for now but NOT saving it over "
                      f"the remembered root transform")
            else:
                try:
                    with open(POS_FILE, "w", encoding="utf-8") as fh:
                        json.dump({"me": my_off, "foe": foe_off}, fh)
                except OSError:
                    pass
            d0 = ((me.xyz()[0] - foe.xyz()[0]) ** 2 + (me.xyz()[2] - foe.xyz()[2]) ** 2) ** 0.5
            print(f"position: from the character objects by motion - ours +0x{my_off:X}, "
                  f"theirs +0x{foe_off:X}, distance now {d0:.0f}")
            return True
        off = find_state_pos_offset()
        if off is not None:
            (me if me_side == "P1" else foe).pos = (me if me_side == "P1" else foe).base + off
            (foe if me_side == "P1" else me).pos = (foe if me_side == "P1" else me).base + off
            d0, _ = (lambda: (((me.xyz()[0] - foe.xyz()[0]) ** 2
                               + (me.xyz()[2] - foe.xyz()[2]) ** 2) ** 0.5, 0))()
            print(f"position: read from each character's own state object at "
                  f"+0x{off:X} (distance now {d0:.0f})")
            return True
        # the shared position block is not trusted any more: its rows picked
        # +0x730 / +0x750 (garbage) in survival mode. Keep what we have and
        # retry the object route a moment later.
        return False

    pos_ok = [fix_pos_rows(quiet=True), 0.0, False]
    if not pos_ok[0]:
        print("  position rows: P2's row not found yet (no match running?); "
              "will keep looking every second")
    me.refresh(); foe.refresh()
    hp = me.get("CurrentHealth")
    print(f"anchors: " + "  ".join(f"{k}=0x{v:012X}" for k, v in anchors.items()))
    my_char = me.get("CurrentCharacter")
    poke_auto, combo_auto = args.poke == "auto", args.combo == "auto"
    if args.poke == "auto":
        args.poke = CHAR_POKE.get(my_char, "punch")
    if args.combo == "auto":
        combo_table = dict(CHAR_COMBOS.get(my_char, {"default": GENERIC_COMBO}))
    elif args.combo.lower() == "none":
        combo_table = {}
    else:
        combo_table = {"default": args.combo}
    print(f"playing {me_side} (char {me.get('CurrentCharacter')}, hp {hp}) "
          f"vs {foe_side} (char {foe.get('CurrentCharacter')}, "
          f"hp {foe.get('CurrentHealth')})")
    if not (0 < hp <= 1000):
        print("  our health reads implausibly; is a match running?")

    holds = dict(HOLD_DIRECTIONS)
    holds["midk"] = (+1, 0) if args.hold_mode == "4way" else (-1, 0)
    if args.hold:
        holds = parse_hold_overrides(args.hold, base=holds)
    print(f"hold mode: {args.hold_mode}  (midk = "
          f"{'6H' if holds['midk'][0] > 0 else '4H'})")
    guard_types = {int(x) for x in args.guard_types.split(",") if x.strip()}
    exceptions = load_json(EXCEPTIONS_FILE, {})
    startup = StartupTable()
    print(f"startup table: {len(startup.table)} (character, move) entries")

    period = 1.0 / args.poll_hz

    facing_state = [True]
    facing_fixes = [0]

    last_probe = [0.0]
    facing_dirty = [True]       # sides may have swapped since the last probe:
                                # a hold threw them, we were thrown, a new
                                # opponent, a round start. 46 of 66 holds in
                                # one survival run had their facing fixed by
                                # the in-hold pretap; the ones with <= 8
                                # frames left had no time for it and missed.
    probe_dirty_n = [0]
    last_throw_seen = [0.0]     # no facing probe right after a throw attempt

    input_lag = [42.0]          # ms from key to walk id, measured by the probe
    input_lag_hist = []
    lag_reported = [False]

    def probe_facing():
        nonlocal me, foe, me_side, foe_side
        """Tap screen-right for a frame while both sides are idle and read
        which way we walked: id 1 = forward (we face right), 2 = back.

        Screen left/right is not world X - on a diagonal stage the X-sign
        guess was wrong most of the time and every "back" hold came out as
        forward+H, a plain guard in 3-way mode. Doing the probe inside the
        hold cost 30 ms and still missed (the walk id shows up 2-3 frames
        after the tap), so it runs here, between attacks, every ~1.5 s.
        """
        foe.refresh()
        foe_idle_before = foe.get("CurrentMove") == 0 and foe.get("MoveKind") == 0
        inj.down(["right"])
        t_key = time.perf_counter()
        time.sleep(0.018)
        inj.up(["right"])
        t0 = time.perf_counter()
        seen = None
        foe_walked = False
        while time.perf_counter() - t0 < min(0.2, input_lag[0] / 1000 + 0.05):
            me.refresh()
            m = me.get("CurrentMove")
            if m in (WALK_FWD, WALK_BACK):
                seen = m
                break
            foe.refresh()
            if foe.get("MoveKind") != 0 or foe.get("CurrentMove") != 0:
                if (foe_idle_before and foe.get("MoveKind") == 0
                        and foe.get("CurrentMove") in FWD_IDS + BACK_IDS):
                    foe_walked = True   # OUR tap moved THEIR character
                break           # something is starting: back to the loop NOW
            time.sleep(0.002)
        if not side_confirmed[0]:
            if seen is not None:
                side_confirmed[0] = True
                side_votes[0] = 0
            elif foe_walked:
                side_votes[0] += 1
                if side_votes[0] >= 2:
                    # the keyboard drives the other object: swap the readers
                    me, foe = foe, me
                    me_side, foe_side = foe_side, me_side
                    pos_mem[0], pos_mem[1] = pos_mem[1], pos_mem[0]
                    side_confirmed[0] = True
                    facing_state[0] = not facing_state[0]
                    print(f"  side: the keyboard moved the {me_side} character - we are "
                          f"{me_side}, not {foe_side}. Readers swapped.")
                    last_probe[0] = time.perf_counter()
                    return None
        if seen is not None:
            lag = (time.perf_counter() - t_key) * 1000
            input_lag_hist.append(lag)
            if len(input_lag_hist) > 15:
                del input_lag_hist[0]
            med = sorted(input_lag_hist)[len(input_lag_hist) // 2]
            if (len(input_lag_hist) == 5 and not lag_reported[0]) or                     (abs(med - input_lag[0]) > 10 and len(input_lag_hist) >= 5):
                lag_reported[0] = True
                print(f"  input lag: a key shows in the game after ~{med:.0f} ms "
                      f"({med / (1000 / 60):.1f} frames; 42 ms in the good sessions)"
                      + ("  - HIGH: holds press direction+H together and need "
                         f"{max(args.min_remaining, round(med / (1000 / 60)) + 3)} frames left"
                         if med > 80 else ""))
            input_lag[0] = med
            new = (seen == WALK_FWD)
            if new != facing_state[0]:
                facing_fixes[0] += 1
                print(f"  facing: now {'RIGHT' if new else 'LEFT'} "
                      f"(walk id {seen} after a right tap)")
            facing_state[0] = new
            facing_dirty[0] = False
        last_probe[0] = time.perf_counter()
        return seen

    def back_names():
        # "back" in screen space from the current facing guess
        return dirs_to_names(-1 if facing_state[0] else +1, 0)

    COMBO_BTN = {"P": "punch", "K": "kick", "T": "throw", "S": "special",
                 "PK": "pk", "HK": "hk", "H": "free"}
    NUMPAD = {"1": (-1, -1), "2": (0, -1), "3": (1, -1), "4": (-1, 0),
              "6": (1, 0), "7": (-1, 1), "8": (0, 1), "9": (1, 1)}

    def parse_combo(text):
        seq = []
        if not text or text.lower() == "none":
            return seq
        for tok in text.split(","):
            tok = tok.strip().upper()
            if not tok:
                continue
            i = 0
            while i < len(tok) and tok[i] in NUMPAD:
                i += 1
            digits, rest = tok[:i], tok[i:]
            btn = COMBO_BTN.get(rest)
            if btn is None and rest == "" and digits:
                btn = None          # a bare direction: the stance entry, held
            elif btn is None:       # with no button (Minato's PPP4 -> Shuffle)
                print(f"combo: unknown token {tok!r}, ignored")
                continue
            # "236P": every direction but the last is tapped first (motion)
            motion = [NUMPAD[c] for c in digits[:-1]]
            last = NUMPAD.get(digits[-1], (0, 0)) if digits else (0, 0)
            seq.append((last, btn, tok, motion))
        return seq

    combo_seqs = {k: parse_combo(v) for k, v in combo_table.items()}
    combo_seqs = {k: v for k, v in combo_seqs.items() if v}
    break_blow_seq = parse_combo("6S")
    try:
        with open(COMBO_STATS_FILE, encoding="utf-8") as fh:
            combo_bank = json.load(fh)
    except (OSError, ValueError):
        combo_bank = {}
    recipe_cache = {}

    def cc_all(char):
        """Entries are a string, or {"seq": ..., "wall": true} for a combo
        that only connects with the OPPONENT against a wall (Minato's
        9K,6P,6P,6P whiffs in the open)."""
        try:
            with open("combo_challenge.json", encoding="utf-8") as fh:
                raw = json.load(fh).get(str(char), [])
        except (OSError, ValueError):
            return []
        out = []
        for e in raw:
            if isinstance(e, dict) and e.get("seq"):
                out.append((e["seq"], bool(e.get("wall"))))
            elif isinstance(e, str) and e:
                out.append((e, False))
        return out

    foe_span = {"x": [None, None], "z": [None, None]}

    def note_foe_pos():
        try:
            fx, _, fz = foe.xyz()
        except Exception:
            return
        for k, v in (("x", fx), ("z", fz)):
            if not (-3e5 < v < 3e5) or v != v:
                return
            lo, hi = foe_span[k]
            foe_span[k] = [v if lo is None else min(lo, v),
                           v if hi is None else max(hi, v)]

    def foe_at_wall(margin=0.12, need=300.0):
        """Is the opponent backed against a wall?

        There is no stage geometry in the layout, so the walls are inferred
        from how far the opponent has actually been seen to travel this
        session: once an axis has a span worth of data, being inside the
        outer eighth of it means the wall is right there. No data yet reads
        as "not at a wall", which is the safe answer."""
        try:
            fx, _, fz = foe.xyz()
        except Exception:
            return False
        for k, v in (("x", fx), ("z", fz)):
            lo, hi = foe_span[k]
            if lo is None or hi - lo < need:
                continue
            if v - lo < (hi - lo) * margin or hi - v < (hi - lo) * margin:
                return True
        return False

    def best_throw(char):
        # (cc_all now yields (sequence, wall-only) pairs)
        """The longest throw the Combo Challenge taught this character.

        A throw-led sequence is no use as a combo recipe - a character in hit
        stun cannot be grabbed - but it is exactly what to press where the bot
        has already decided to throw: the T-first on an idle opponent, and the
        throw punish after their whiff. Minato's 214T is a four-part command
        throw and the plain T beside it is small change."""
        best = None
        for t, _w in cc_all(char):
            toks = t.split(",")
            if toks[0].endswith("T") and (best is None or len(toks) > len(best.split(","))):
                best = t
        return best

    def press_best_throw(bt):
        """Each part goes in when the previous part's animation changes.

        A fixed interval does not fit: Minato's four-part 214T ran 0.22, 0.51
        and 0.48 s apart, so 0.25 s everywhere pressed the last two early and
        the chain dropped."""
        for n_t, st_t in enumerate(parse_combo(bt)):
            if n_t:
                mv_t = me.get("CurrentMove")
                t_t = time.perf_counter()
                while time.perf_counter() - t_t < 0.9:
                    me.refresh()
                    if me.get("MoveKind") == 0:
                        break             # the throw ended: nothing to chain
                    if me.get("CurrentMove") != mv_t:
                        break             # next part of the animation: press now
                    time.sleep(0.003)
            combo_press(st_t)

    def cc_extra(char):
        """Combos the game's own Combo Challenge taught, cleared by
        comboreplay and written to combo_challenge.json. They join the
        character's "default" pool and the net-damage bandit prices them
        against everything else."""
        return cc_all(char)

    def choose_recipe(key):
        """Round-robin until every candidate has 3 tries, then the best mean
        damage, with one try in seven spent on the runner-up so a lucky
        early sample cannot lock a weaker string in."""
        pool = RECIPE_POOL.get(my_char, GENERIC_POOL).get(key) if combo_auto else None
        if pool is not None and key == "default":
            # throw-led sequences are not combo material (no grabbing a
            # character in hit stun); best_throw uses those instead
            at_wall = foe_at_wall()
            pool = pool + [t for t, w in cc_extra(my_char)
                           if t not in pool and not t.split(",")[0].endswith("T")
                           and (at_wall or not w)]
        if not pool:
            return None, None
        stats = combo_bank.setdefault(str(my_char), {}).setdefault(str(key), {})
        for r in pool:
            stats.setdefault(r, [0, 0])
        least = min(pool, key=lambda r: stats[r][0])
        if stats[least][0] < 3:
            r = least
        else:
            ranked = sorted(pool, key=lambda r: -(stats[r][1] / max(1, stats[r][0])))
            total = sum(stats[r][0] for r in pool)
            r = ranked[1] if (len(ranked) > 1 and total % 7 == 0) else ranked[0]
        if r not in recipe_cache:
            recipe_cache[r] = parse_combo(r)
        return r, recipe_cache[r]

    def bank_result(recipe, key, dealt):
        stats = combo_bank.setdefault(str(my_char), {}).setdefault(str(key), {})
        stats.setdefault(recipe, [0, 0])
        stats[recipe][0] += 1
        stats[recipe][1] += dealt
        try:
            with open(COMBO_STATS_FILE, "w", encoding="utf-8") as fh:
                json.dump(combo_bank, fh, indent=1, sort_keys=True)
        except OSError:
            pass
    if combo_seqs:
        who = CHAR_NAMES.get(my_char, f"char {my_char}")
        pool = RECIPE_POOL.get(my_char, GENERIC_POOL) if combo_auto else None
        if pool:
            print(f"offence: poke = {args.poke}; strings for {who} (scored by net damage): "
                  + "; ".join(f"after {k}: {' | '.join(v)}" for k, v in pool.items()))
        else:
            print(f"offence: poke = {args.poke}; combos for {who}: "
                  + "; ".join(f"{k}: {combo_table[k]}" for k in combo_seqs))
    combo = {"i": 0, "last_mv": None, "t": 0.0, "hp0": None, "hits": 0,
             "seq": [], "opener": None}
    combo_stats = {"started": 0, "hits": 0, "dmg": 0, "by_len": {}}

    def step_reach(step):
        """How far this follow-up still connects. A plain P is the short one
        (the number CHAR_PUNCH_REACH was measured for); S is Fatal Rush, which
        runs the gap shut by itself (a P,S,S,S,S was cut at 155 and the S
        would have hit), a kick reaches further than a punch, and a 6-move
        steps in as it comes out."""
        tok = step[2] if len(step) > 2 else "P"
        base = CHAR_PUNCH_REACH.get(my_char, 150)
        if "S" in tok:
            return 400.0                 # Fatal Rush closes the distance
        r = base
        if "K" in tok:
            r += 30
        if tok[:1] == "6":
            r += 40
        return r

    in_string = [False]            # a follow-up, not the first input

    def combo_press(step):
        (dx, dy), btn, tok, motion = (step + ([],))[:4]
        if btn is None:
            # bare direction: hold it through the previous move's recovery so
            # the stance transition takes, then let go
            names_s = dirs_to_names(dx if facing_state[0] else -dx, dy)
            if names_s:
                inj.down(names_s)
                time.sleep(0.18)
                inj.up(names_s)
                dir_touched[0] = time.perf_counter()
            return tok
        dash = bool(motion) and motion[-1] == ((dx, dy))
        for mdx, mdy in motion:                      # 236P: tap 2, tap 3, then 6+P
            mn = dirs_to_names(mdx if facing_state[0] else -mdx, mdy)
            inj.down(mn); time.sleep(0.033); inj.up(mn); time.sleep(0.017)
        sdx = dx if facing_state[0] else -dx
        names = dirs_to_names(sdx, dy)
        if not names:
            inj.up(["left", "right", "up", "down"])   # S with back held is 4S
            quiet = time.perf_counter() - dir_touched[0]
            if quiet < 0.10:                           # let the 4/6 buffer expire
                time.sleep(0.10 - quiet)
        horiz, vert = dirs_to_names(sdx, 0), dirs_to_names(0, dy)
        if dash and horiz:
            # 66P is a RUN and then a punch, and the run is most of its range.
            # Tapping forward and pressing 17 ms later produced the right move
            # a step from where it started (the Combo Challenge's 66P fell
            # short of the post until the run-up was held for ~0.14 s).
            inj.down(horiz)
            time.sleep(0.13)
            inj.down(vert + [btn])
        elif horiz and vert and dy < 0:
            # A DOWN diagonal needs both directions in place before the
            # button. Sent alongside it the game kept only the horizontal:
            # Minato's 3K came out as 6K and 1P/7P as 4P, while the UP
            # diagonals (9P, 9K) were fine.
            inj.down(horiz + vert)
            time.sleep(0.05)
            inj.down([btn])
        else:
            if horiz:
                inj.down(horiz)        # 6P/4P and the horizontal half of an
                # a direction inside a string needs longer than the 17 ms that
                # works from neutral: PPP>4P came out as the plain fourth P
                time.sleep(0.05 if in_string[0] else 0.017)
            # the vertical goes down WITH the button (down alone a frame ahead
            # is a sidestep: 2T came out as id 32)
            inj.down(vert + [btn])
        time.sleep(args.press)
        inj.up([btn])
        if names:
            inj.up(names)
        return tok

    def combo_reset(why):
        if combo["hp0"] is not None:
            dealt = max(0, combo["hp0"] - foe.get("CurrentHealth"))
            if dealt > 300:
                dealt = 0               # a stale object read (25 "combos" at 143)
            combo_stats["dmg"] += dealt
            combo_stats["by_len"].setdefault(combo["hits"], [0, 0])
            combo_stats["by_len"][combo["hits"]][0] += 1
            combo_stats["by_len"][combo["hits"]][1] += dealt
            taken = max(0, (combo.get("myhp0") or me.get("CurrentHealth")) - me.get("CurrentHealth"))
            if combo["hits"]:
                print(f"        + combo over ({why}): {combo['hits']} extra input(s), "
                      f"{dealt} dmg since the opener"
                      + (f", we lost {taken}" if taken else "")
                      + (f"  [{combo['recipe']}]" if combo.get("recipe") else ""))
            if combo.get("recipe") and combo["hits"] and dealt <= 500 and taken <= 500:
                # net: the CPU holds the second hit of a string often (8294/8300
                # cost 300+ in one match) - a string that gets us held scores low.
                # A garbage health read (object swap) must not enter the bank
                bank_result(combo["recipe"], combo.get("rkey", "default"), dealt - taken)
        # the move we were in when the string ended must not open a NEW
        # string a tick later: after "6P not accepted 6x" during our 8K, a
        # fresh combo started on the same 8K, pressed 8K again into the
        # buffer, and the 6P that followed came out as 8K (191)
        combo["skip_mv"] = me.get("CurrentMove") if me.get("CurrentMove") not in (0,) else None
        combo.update(i=0, last_mv=None, t=0.0, hp0=None, hits=0, seq=[], opener=None,
                     recipe=None, rkey=None)

    def poke(why):
        """Strike into the space the CPU is about to throw from. The punch
        (15 f) inside its reach, the kick (16 f, longer) beyond it."""
        d0, _ = distance()
        if d0 > args.jab_reach:
            return None                    # the far kick poke went 0 for 14
        if foe.get("CurrentHealth") <= 0 or me.get("CurrentHealth") <= 0:
            return None                    # round over: 3 "no reaction" pokes
        btn = args.poke
        mv0 = me.get("CurrentMove")
        pre = {"mv": mv0, "mt": me.get("MoveType"), "ph": 0,
               "fmv": foe.get("CurrentMove"), "fph": foe.get("Phase")}
        if btn[0] in NUMPAD:                   # "4P": direction a frame ahead
            step = parse_combo(btn)
            if step:
                combo_press(step[0])
            else:
                jab(inj, "punch", press=args.press)
        else:
            jab(inj, btn, press=args.press)
        t = time.perf_counter()
        last_poke[0] = t
        pokes[0] += 1
        by_kind.setdefault("poke", [0, 0])[1] += 1
        fire("poke", f"* poke {why}", mv0, None, pre)
        last_action[:] = ["poke", t]
        d, _ = distance()
        print(f"  *    poke ({why:<8}) -> {btn:<6} dist={d:.0f}")
        return t

    def backdash():
        """Tap back twice (a backdash), then keep walking back a moment.
        Running throws (8144/8149/8340) start 170+ away and travel; the
        grab happens where we WERE. Punching them fails - a jab does not
        reach 170 - so give ground instead."""
        b = back_names()
        # first tap doubles as the facing oracle: walk/dash id 1/3 means we
        # just stepped FORWARD - flip and start over with the other key.
        # Two runners grabbed us because the "backdash" went toward them.
        inj.down(b); time.sleep(0.030); inj.up(b)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.050:
            me.refresh()
            m = me.get("CurrentMove")
            if m in FWD_IDS:
                facing_state[0] = not facing_state[0]
                facing_fixes[0] += 1
                b = back_names()
                inj.down(b); time.sleep(0.030); inj.up(b)
                break
            if m in BACK_IDS:
                break
            time.sleep(0.002)
        time.sleep(0.030)
        inj.down(b); time.sleep(0.030); inj.up(b)
        time.sleep(0.020)
        # then keep walking back for as long as the threat lasts: a fixed
        # 0.2 s stopped two frames before 8284 arrived (22-frame runner)
        inj.down(b)
        t_end = time.perf_counter() + 1.20
        threat = foe.get("CurrentMove")
        last_fr = foe.get("CurrentMoveFrame")
        while time.perf_counter() < t_end:
            foe.refresh()
            fk, fm = foe.get("MoveKind"), foe.get("CurrentMove")
            f_now = foe.get("CurrentMoveFrame")
            if fk not in (MK_THROW, 2, 3) and fm != threat:
                break
            if fk == 4:                             # it grabbed (someone)
                break
            if fk == MK_THROW and (fm != threat and fm != 0 or f_now < last_fr):
                break           # a NEW throw (other id, or the same one again -
                                # 8340 x4 back to back): let the main loop answer
            last_fr = f_now
            time.sleep(0.005)
        time.sleep(0.05)
        inj.up(b)

    def sidestep():
        """A TAP of up is a sidestep; holding down is a crouch (id 13),
        which is what the earlier 'duck' really did."""
        inj.down(["up"]); time.sleep(0.040); inj.up(["up"])

    oracle_seen = [None]        # ms from horizontal press to walk id, last hold

    def oracle_hold(kind, button="free", wait=0.040):
        """Hold with the horizontal pressed one frame early as a facing probe.

        Screen left/right is not world X, and the sides swap all the time
        (sidesteps, throws), so the facing guess is often stale. The game
        answers within a frame of a direction going down: walk id 2 means we
        are stepping BACK (the direction a hold wants), 1 means forward -
        mirrored, so release, flip, press the other side. Then the vertical
        component and H go down together. A one-frame horizontal lead did
        not stop holds in the standing tests; the vertical must not lead
        (down alone starts a crouch, and H then becomes a crouch guard).
        """
        dx, dy = holds[kind]
        sdx = dx if facing_state[0] else -dx
        horiz = dirs_to_names(sdx, 0)
        flipped = False
        if horiz:
            me.refresh()
            m0 = me.get("CurrentMove")     # a walk id here is the tail of the
            stale = m0 in FWD_IDS + BACK_IDS   # zoning walk, not our press
            inj.down(horiz)
            t0 = time.perf_counter()
            walk = None
            while time.perf_counter() - t0 < wait:
                me.refresh()
                m = me.get("CurrentMove")
                if m not in FWD_IDS + BACK_IDS:
                    stale = False
                elif not stale or m != m0:
                    walk = m
                    break
                time.sleep(0.001)
            oracle_seen[0] = None if walk is None else (time.perf_counter() - t0) * 1000
            if walk is None:
                oracle_miss[0] += 1        # the walk id needs ~42 ms to show
            # the hold's horizontal is BACK for 7H/4H/1H but FORWARD for the
            # 4-way mid-kick 6H: a forward walk is only "mirrored" when we
            # meant to go back (6H read as mirrored -> flipped -> 4H = 154,
            # 0 for 4 mid kicks in the first 4-way match)
            mirrored = (walk in FWD_IDS) if dx < 0 else (walk in BACK_IDS)
            if walk is not None:
                swap_ref[0] = None             # facing confirmed as of now
            if mirrored:                       # we walked the wrong way
                inj.up(horiz)
                facing_state[0] = not facing_state[0]
                facing_fixes[0] += 1
                flipped = True
                sdx = -sdx
                horiz = dirs_to_names(sdx, 0)
                inj.down(horiz)
                time.sleep(0.020)          # a frame, or S alone = Fatal Rush
        vert = dirs_to_names(0, dy)
        if vert and vert_lead[0]:
            # "down alone one call early starts a crouch and H becomes a
            # crouch guard" held for the characters this was measured on, but
            # Minato's 1H came out as 4H 49 times in 112 (the catch animation
            # was the mid-punch one), and her low holds fell to 47%. When that
            # happens the vertical goes down WITH the horizontal instead, a
            # frame before H - the same recipe her 3K needed.
            inj.down(vert)
            time.sleep(0.017)
            inj.down([button])
        else:
            inj.down(vert + [button])
        pressed_at = time.perf_counter()
        time.sleep(args.press)
        inj.up([button])
        inj.up(vert + horiz)
        return pressed_at, flipped

    vert_lead = [False]     # send the hold's vertical a frame early
    wrong_hold = [0, 0]     # [low holds that caught with another hold, tried]
    pos_warn = [0.0]
    swap_ref = [None]       # unit vector foe-me (world XZ) frozen when a throw /
                            # hold throw / knockdown starts; compared when it ends
    swap_flips = [0]
    pos_zero = [time.perf_counter()]    # last time the distance was > 8
    pos_last = [0.0, time.perf_counter()]   # last distinct distance, when

    pos_suspect = [0.0]     # last time the distance read like nonsense

    def distance():
        """Range and which way we face, from the two positions.

        No orientation field has been identified, so facing is "the foe is
        to our right". That flips spuriously when the characters stand side
        by side (dx near zero, e.g. mid-sidestep or after a throw): three 4H
        attempts came out as 6H that way. Hysteresis: only change our mind
        when the X gap is clearly one way or the other.
        """
        mx, my_, mz = me.xyz()
        fx, fy, fz = foe.xyz()
        if last_probe[0] == 0.0 and abs(fx - mx) > 40:
            facing_state[0] = fx > mx          # only until the first probe
        d = ((mx - fx) ** 2 + (mz - fz) ** 2) ** 0.5
        if not pos_ok[0] and time.perf_counter() - pos_ok[1] > 1.0:
            pos_ok[1] = time.perf_counter()
            if fix_pos_rows():
                pos_ok[0] = True
                mx, my_, mz = me.xyz()
                fx, fy, fz = foe.xyz()
                d = ((mx - fx) ** 2 + (mz - fz) ** 2) ** 0.5
        if d > 8:
            pos_zero[0] = time.perf_counter()
        if pos_ok[0] and 0 < d < POS_MAX_D:
            note_foe_pos()
        if abs(d - pos_last[0]) > 0.5 or foe.get("MoveKind") == 0:
            pos_last[:] = [d, time.perf_counter()]
        frozen = time.perf_counter() - pos_last[1] > 1.5   # a live foe never holds still this long
        stuck = time.perf_counter() - pos_zero[0] > 3.0 or frozen
        if frozen:
            pos_last[1] = time.perf_counter()
        if d > POS_MAX_D or d != d:
            pos_suspect[0] = time.perf_counter()
        if (d > POS_MAX_D or d != d or stuck) and time.perf_counter() - pos_warn[0] > 5.0 \
                and me.get("CurrentHealth") > 0 and foe.get("CurrentHealth") > 0:
            if stuck:
                pos_zero[0] = time.perf_counter()
            # a 9984 "distance" (char 20 vs char 6) means one position read
            # is garbage: the pos anchor does not hold for this pairing
            pos_warn[0] = time.perf_counter()
            print(f"  [!] position read implausible: me=({mx:.0f},{my_:.0f},{mz:.0f}) "
                  f"foe=({fx:.0f},{fy:.0f},{fz:.0f}) dist={d:.0f} - re-detecting "
                  f"the position rows")
            if fix_pos_rows():
                mx, my_, mz = me.xyz()
                fx, fy, fz = foe.xyz()
                d = ((mx - fx) ** 2 + (mz - fz) ** 2) ** 0.5
        # Side swaps. Screen left/right is not world X, but a throw, a hold
        # throw or a knockdown that puts the foe on our OTHER side reverses
        # the world vector between us while the camera stays where it was.
        # Slow circling (sidesteps) rotates the camera along and must not
        # count, so the vector is only compared across such an event: frozen
        # when it starts, checked when both are back on their feet. 303 of
        # 423 holds in one survival run went in with a stale facing.
        if pos_ok[0] and 40 < d < POS_MAX_D:
            ux, uz = (fx - mx) / d, (fz - mz) / d
            mm, fm = me.get("CurrentMove"), foe.get("CurrentMove")
            in_event = (me.get("MoveType") in (MT_THROWN, MT_HOLD_HIT)
                        or foe.get("MoveType") in (MT_THROWN, MT_HOLD_HIT)
                        or foe.get("MoveKind") == 4
                        or 125 <= mm <= 135 or 125 <= fm <= 135)
            if in_event:
                if swap_ref[0] is None:
                    swap_ref[0] = (ux, uz)
            elif swap_ref[0] is not None:
                rx, rz = swap_ref[0]
                swap_ref[0] = None
                if rx * ux + rz * uz < -0.3:      # they came out the other side
                    facing_state[0] = not facing_state[0]
                    swap_flips[0] += 1
        return d, facing_state[0]

    # ---------------------------------------------------------------- probe
    if args.probe:
        print("\nprobe: prints when the opponent's move, kind or phase "
              "changes while MoveKind != 0 (strike 3, throw 16, hold 5). "
              "ctrl-c to stop\n")
        last = None
        try:
            while True:
                foe.refresh()
                kind = foe.get("MoveKind")
                if kind == 0:
                    last = None
                    time.sleep(1 / 240)
                    continue
                mv, ph = foe.get("CurrentMove"), foe.get("Phase")
                if (mv, kind, ph) != last:
                    last = (mv, kind, ph)
                    fr = foe.get("CurrentMoveFrame")
                    st = foe.get("StrikeType")
                    hml = foe.get("HighMidLowGround")
                    d, right = distance()
                    known = startup.get(foe.get("CurrentCharacter"), mv)
                    rem = f"{known - fr:>3}" if known else "  ?"
                    what = {3: "STRIKE", 2: "STRIKE", 16: "throw",
                            5: "hold"}.get(kind, f"kind{kind}")
                    print(f"  move={mv:<6} {what:<6} phase={ph} frame={fr:<3} "
                          f"remaining={rem} {STRIKE_TYPE.get(st, '?'):<11} "
                          f"h/m/l={hml} dist={d:6.1f} me={'R' if right else 'L'}")
                    if ph == PH_ACTIVE and kind in MK_STRIKES and startup.learn(
                            foe.get("CurrentCharacter"), mv, fr):
                        print(f"      learned: char {foe.get('CurrentCharacter')} "
                              f"move {mv} goes active at frame {fr}")
                time.sleep(1 / 240)
        except KeyboardInterrupt:
            pass
        finally:
            startup.save()
            proc.close()
        return

    # ------------------------------------------------------------------ run
    if not enable_high_res_timer():
        print("  warning: timer resolution not raised; reactions ~4 frames late")
    inj = make_injector("null" if args.dry_run else args.injector)
    # DOA reads "4S"/"6S" as a SEQUENCE: a back or forward input a few frames
    # before S turns the Fatal Rush into a Break Hold (8415/8416, or the 270
    # refusal pose) / Break Blow (8397). Remember when a direction key was
    # last touched so a directionless input can wait for neutral.
    dir_touched = [0.0]
    _DIRS = {"left", "right", "up", "down"}
    _orig_down, _orig_up = inj.down, inj.up
    def _down(names):
        if _DIRS & set(names):
            dir_touched[0] = time.perf_counter()
        return _orig_down(names)
    def _up(names):
        if _DIRS & set(names):
            dir_touched[0] = time.perf_counter()
        return _orig_up(names)
    inj.down, inj.up = _down, _up
    print(f"injector: {type(inj).__name__}"
          f"{'  (DRY RUN)' if args.dry_run else ''}")
    if args.test_combo:
        # ---- Training-mode check -------------------------------------------
        # Uses combo_press, the same code a match uses, so what this prints is
        # what the bot actually does. Expected ids come from the character's
        # own calibration table in commands.json.
        try:
            with open("commands.json", encoding="utf-8") as fh:
                _cm = json.load(fh)
            _sec = (_cm.get("chars") or {}).get(str(my_char), {})
        except (OSError, ValueError):
            _sec = {}
        expect = {k: v.get("move") for k, v in _sec.items()
                  if isinstance(v, dict) and v.get("move")}

        if args.test_combo == "all":
            strings = list(RECIPE_POOL.get(my_char, GENERIC_POOL).get("default") or [])
            for t, _w in cc_all(my_char):
                if t not in strings:
                    strings.append(t)
        else:
            strings = [args.test_combo]

        print(f"test-combo: {len(strings)} string(s) for "
              f"{CHAR_NAMES.get(my_char, my_char)}. Stand in Training with the "
              f"dummy in front of you. Ctrl-C to stop.\n")
        for text in strings:
            for run_n in range(args.test_repeat):
                t_wait = time.perf_counter()
                while time.perf_counter() - t_wait < 4.0:     # both on their feet
                    me.refresh(); foe.refresh()
                    if (me.get("MoveKind") == 0 and foe.get("MoveKind") == 0
                            and me.get("CurrentMoveFrame") >= 0):
                        break
                    time.sleep(0.005)
                time.sleep(0.25)
                steps = parse_combo(text)
                print(f"  {text}" + (f"  (run {run_n + 1})" if args.test_repeat > 1 else ""))
                hp0 = foe.get("CurrentHealth")
                for st in steps:
                    tok = st[2]
                    before = me.get("CurrentMove")
                    combo_press(st)
                    got, t_s = [], time.perf_counter()
                    while time.perf_counter() - t_s < 0.9:
                        me.refresh()
                        mv_n, k_n = me.get("CurrentMove"), me.get("MoveKind")
                        if k_n != 0 and mv_n != before and (not got or got[-1] != mv_n):
                            got.append(int(mv_n))
                        # press the next token as soon as this one is out, the
                        # way the engine does. Waiting for neutral put the
                        # second part of a four-part throw 0.9 s late, where
                        # the demo pressed it after 0.22 s.
                        if got and time.perf_counter() - t_s > 0.12:
                            break
                        if k_n == 0 and time.perf_counter() - t_s > 0.4:
                            break
                        time.sleep(0.002)
                    want = expect.get(tok)
                    if tok.strip("0123456789") == "":
                        mark = "(stance entry, no button)"
                    elif not got:
                        mark = "NOTHING CAME OUT"
                    elif want is None:
                        mark = "(this token is not in the calibration)"
                    elif want in got:
                        mark = "ok"
                    else:
                        mark = f"WRONG - expected {want}"
                    print(f"      {tok:<6} -> {'>'.join(map(str, got)) or '-':<22} {mark}")
                dealt = hp0 - foe.get("CurrentHealth")
                print(f"      damage to the dummy: {dealt if 0 <= dealt < 500 else '?'}\n")
                time.sleep(1.0)
        inj.release_all()
        return

    print("running. ctrl-c to stop\n")

    armed = True
    last_fire = 0.0
    count = guards = landed = skipped_unknown = skipped_late = 0
    skipped_stun = 0
    learned_now = 0
    pending = []            # dicts, see fire()
    by_kind = {}
    by_rem = {}
    my_anim = {}            # (kind, landed) -> {our move id ~120ms later: n}
    no_reaction = 0
    last_phase, last_move = None, None
    episode = None          # the strike currently in startup, see below
    unknown_seen = {}       # move id -> times guarded with no startup yet
    keys_held_count = [0]   # holds fired while a bound key was already down
    skipped_note = [None]   # last foe move we reported skipping
    # where OUR health goes: (foe move id, what we were doing) -> damage
    dmg_by_move = {}
    dmg_by_state = {}
    history = []            # (t, foe move, foe kind, foe phase, my move, my type)
    throw_dumps = [0, None, 0.0]   # count, last foe move dumped, when
    last_my_hp = me.get("CurrentHealth")
    last_action = ["none", 0.0]     # what we last did and when
    ducked = punished = 0
    approach_hits = [0]
    guarding = None         # our non-blocking guard, see the tick loop
    gauge_seen = {"me": [10**9, -1], "foe": [10**9, -1]}   # min, max
    breaks = [0, 0]         # break holds, break blows thrown
    gauge_spent = [0, 0]    # [spent this round, field value at last check]

    def gauge_now():
        """The Break Gauge itself: +0x5B4 (u16). Found by watching a live
        match: pinned at 200 with the on-screen bar full, the other side
        rising 104 -> 147 as it took hits, 100 at the start of a match,
        carried over between rounds; the gauge hunt saw it drop ~96 on a
        Break Hold. +0x584 was only a running total and mis-timed 4S badly."""
        return me.get("BreakMeter")
    was_intro = [False]     # MoveType 17 = round intro, inputs locked
    intro_tap = [0.0, 0]    # last guard tap in the intro, printed once
    zoning = None           # the back key we are holding to keep distance
    zone_t0 = [0.0, 0.0]    # when zoning started, distance then (wall check)
    precrouch = None        # keys held while we sit under a fast throw's reach
    danger_cache = {"char": None, "t": 0.0, "reach": None, "mv": None, "su": None}
    danger_stats = [0, 0]   # pre-crouch episodes, danger back-offs
    # Throw them first. Inside a fast throw's reach nothing reactive works
    # (7 frames + 2 of lag), a poke is a 15-frame race we lose, but our own
    # neutral T is ~5 frames: if they stand there, we grab them; if they
    # start theirs in the same window, the earlier one wins. Scored per
    # opponent character (close_throw.json) and dropped under 30%.
    CT_FILE = "close_throw.json"
    try:
        with open(CT_FILE, encoding="utf-8") as fh:
            ct_stats = json.load(fh)
    except (OSError, ValueError):
        ct_stats = {}
    close_throw = {"t": 0.0, "hp0": None, "myhp0": None, "done": True, "mode": "idle"}

    def ct_key(mode):
        # "walk": T pressed right after letting go of the back key - the game
        # may read it as 4T; scored apart from the standing version (15/17).
        # Keyed by OUR character too: Mai's T went 18/25 on char 2, Kula's
        # went 0/13 in her first match (different range and speed)
        base = f"{my_char}:{fchar}"
        return base if mode == "idle" else f"walk:{base}"

    def ct_allowed(mode="idle"):
        ok, n = ct_stats.get(ct_key(mode), [0, 0])
        return n < 4 or ok / n >= 0.3

    def ct_resolve(ok):
        close_throw["done"] = True
        mode = close_throw["mode"]
        st = ct_stats.setdefault(ct_key(mode), [0, 0])
        st[0] += 1 if ok else 0
        st[1] += 1
        print(f"        T first{' out of the walk' if mode == 'walk' else ''} "
              f"{'grabbed them' if ok else 'did not connect'}  "
              f"({st[0]}/{st[1]} vs char {fchar}"
              + ("" if ct_allowed(mode) else " - giving it up against this one") + ")")
        try:
            with open(CT_FILE, "w", encoding="utf-8") as fh:
                json.dump(ct_stats, fh, indent=1, sort_keys=True)
        except OSError:
            pass
    zone_flip = [0.0, 0]    # last facing flip from the zoning walk, count
    salvage = [0.0, None]   # until when to watch a hold turning into a guard
    break_need = [args.gauge_max // 2]   # gauge estimate a 4S needs; learned
    oracle_miss = [0]       # holds whose facing probe saw no walk id in time
    rise_tap = [None, 0.0]  # (down-state id we tapped H for, when we entered it)
    hunt_ring = []          # (t, me.buf, foe.buf) for the gauge hunt
    hunt = None             # {"side", "t", "before"} while a Break Hold is watched
    try:
        with open(THROWS_FILE, encoding="utf-8") as fh:
            throws_seen = json.load(fh)
    except (OSError, ValueError):
        throws_seen = {}
    try:
        with open(OH_FILE, encoding="utf-8") as fh:
            oh_seen = {k: set(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        oh_seen = {}
    oh_stats = [0, 0]       # OH sidesteps taken, OH moves learned this session
    try:
        with open(NOHOLD_FILE, encoding="utf-8") as fh:
            nohold_seen = {k: set(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        nohold_seen = {}
    nohold_cnt = {}
    try:
        with open(STUN_FILE, encoding="utf-8") as fh:
            stun_tab = {int(k): v for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        stun_tab = {}

    def stun_dead(rid):
        """A stun animation in which 3+ holds were tried and none registered
        (25773 / 32057 / 32043 in the first session) is not holdable:
        stop wasting inputs there."""
        ok, n = stun_tab.get(rid, (0, 0))
        # 24099 went 1 for 15: one lucky catch must not keep a stun alive
        return (n >= 3 and ok == 0) or (n >= 10 and ok / n < 0.15)

    try:
        with open(ANSWERS_FILE, encoding="utf-8") as fh:
            throw_ans = json.load(fh)
    except (OSError, ValueError):
        throw_ans = {}
    last_answer = {"mv": None, "ans": None, "t": 0.0, "done": True}

    def answer_stats(ch, tmv):
        return throw_ans.setdefault(str(ch), {}).setdefault(str(tmv), {})

    def danger_reach():
        """Reach of this opponent's fastest unanswerable throw, or None.

        char 2's 8137: 7 frames, grabs from 146, crouch 1/9, sidestep 0/3,
        every break input 0/x - nothing reactive beats it with 2 frames of
        input lag, and it lost two survival rounds in a row. What works is
        not standing in its reach: back off there instead of poking, and
        when the wall stops us, sit in a crouch so the standing throw
        whiffs. A throw counts once its startup is learned at <= 8 frames
        and its answers have failed 4+ times at under 30%."""
        now = time.perf_counter()
        if danger_cache["char"] == fchar and now - danger_cache["t"] < 1.0:
            return danger_cache["reach"]
        best, best_mv, best_su = None, None, None
        for mv_s, ent in throws_seen.get(str(fchar), {}).items():
            su = startup.get(fchar, int(mv_s))
            reach = ent.get("reach")
            hi = throw_hi(fchar, int(mv_s))
            if reach is None or (su is None and hi < 2) or (
                    su is not None and su > 8 and hi < 2
                    and throw_cost(fchar, int(mv_s)) < 300):
                continue
            st = answer_stats(fchar, int(mv_s))
            n = sum(v[1] for v in st.values())
            ok = sum(v[0] for v in st.values())
            # either nothing answers it, or it has simply cost us too much:
            # a multi-part command throw that lands one time in three still
            # takes a third of the bar with it
            bad = (n >= 4 and ok / n < 0.3) or hi >= 2 or (
                n >= 6 and ok / n < 0.6 and throw_cost(fchar, int(mv_s)) >= 300)
            if bad and (best is None or reach > best):
                best, best_mv, best_su = reach, int(mv_s), su
        danger_cache.update(char=fchar, t=now, reach=best, mv=best_mv, su=best_su)
        return best

    def pick_answer(ch, tmv, default, options=("duck", "back", "side", "lowkick", "hopkick"),
                    explore=False):
        # "hopkick": 8K. Up + K together - a jumping/hop kick where the
        # character has one is airborne and cannot be thrown; where it does
        # not, up alone is a sidestep and the K a sidestep attack. Scored
        # like the rest, so a useless one is dropped after two tries.
        # "lowkick": 2K. A low attack puts us in crouching status from its
        # first frame, and a standing throw grabs nothing crouching - the
        # crouch walk needs 4+ frames to get there and lost to the 7-frame
        # 8137 13 times out of 14. Tried once duck/back/side have all failed.
        """duck beat every runner so far, but char 25's 8036 grabbed a
        croucher 8 times in a row. Score each option by (ok+1)/(n+2) and take
        the best; a fresh move keeps the default until it fails twice."""
        st = answer_stats(ch, tmv)
        d = st.get(default, [0, 0])
        if explore and "lowkick" in options:
            # A default that is merely good blocks the better option for good:
            # "duck" scores 82% on the slow throws, never drops under the 0.5
            # bar, so 2K was never tried there - while on the fast throws,
            # where it IS the default, 2K is 726/741 (98%) against duck's
            # 508/736. So: every 6th answer to this move, spend one try on
            # the least-tested option until it has 5 of its own.
            for opt in ("lowkick",):
                ok_o, n_o = st.get(opt, [0, 0])
                if opt == default or n_o >= 5:
                    continue
                if n_o >= 2 and ok_o == 0:
                    continue      # 2K went 0/3 against char 21's 17-frame
                                  # 8144 while the duck was 272/295: the
                                  # question is answered, stop paying for it
                tries = sum(v[1] for v in st.values())
                if tries >= 3 and tries % 6 == 5:
                    return opt
        if d[1] < 2 or (d[0] + 1) / (d[1] + 2) >= 0.5:
            return default
        best = default
        best_s = (d[0] + 1) / (d[1] + 2)
        for opt in options:
            ok, n = st.get(opt, [0, 0])
            sc = (ok + 1) / (n + 2)
            if sc > best_s + 1e-9:
                best, best_s = opt, sc
        return best

    def record_answer(ok, dmg=0):
        la = last_answer
        if la["done"] or la["mv"] is None:
            return
        la["done"] = True
        st = answer_stats(fchar, la["mv"])
        st.setdefault(la["ans"], [0, 0])
        st[la["ans"]][0] += 1 if ok else 0
        st[la["ans"]][1] += 1
        if not ok and dmg > 0:
            ent = throw_ent(fchar, la["mv"], create=True)
            ent["dmg"] = ent.get("dmg", 0) + int(dmg)
            save_throw_dmg()
        # one grab from a three-part command throw costs 120: waiting for a
        # second failure before changing the answer pays for the lesson twice
        if not ok and (st[la["ans"]][1] >= 2 or dmg >= 60):
            ok_, n_ = st[la["ans"]]
            nxt = pick_answer(fchar, la["mv"], la["ans"])
            if nxt != la["ans"]:
                print(f"  !    {la['mv']}: '{la['ans']}' failed {n_ - ok_}/{n_} - "
                      f"answering it with '{nxt}' from now on (throw_answers.json)")
        try:
            with open(ANSWERS_FILE, "w", encoding="utf-8") as fh:
                json.dump(throw_ans, fh, indent=1, sort_keys=True)
        except OSError:
            pass

    try:
        with open(TDMG_FILE, encoding="utf-8") as fh:
            throw_dmg = json.load(fh)       # {char: {move: damage it has cost us}}
    except (OSError, ValueError):
        throw_dmg = {}

    def throw_ent(ch, tmv, create=False):
        """create=False must not touch the table: a read that inserted an
        empty entry filled throw_damage.json with every throw ever seen,
        garbage character ids included."""
        if not create:
            ent = throw_dmg.get(str(ch), {}).get(str(tmv))
            if isinstance(ent, dict):
                return ent
            return {"dmg": int(ent), "hi": 0} if isinstance(ent, int) else {}
        ent = throw_dmg.setdefault(str(ch), {}).setdefault(str(tmv), {})
        if not isinstance(ent, dict):                  # the first format was a bare int
            ent = {"dmg": int(ent), "hi": 0}
            throw_dmg[str(ch)][str(tmv)] = ent
        return ent

    def throw_cost(ch, tmv):
        return int(throw_ent(ch, tmv).get("dmg", 0))

    def throw_hi(ch, tmv):
        """How often this throw has caught us inside our own attack. Those
        are hi-counter grabs: unbreakable, and the answer never gets to
        start (char 7's 8108, 12 f and 85% against a duck, grabbed us twice
        at frame 7 with 5 frames left because we were in the P+K recovery).
        Nothing reactive fixes that - the poke has to stop happening in its
        reach."""
        return int(throw_ent(ch, tmv).get("hi", 0))

    def save_throw_dmg():
        try:
            with open(TDMG_FILE, "w", encoding="utf-8") as fh:
                json.dump(throw_dmg, fh, indent=1, sort_keys=True)
        except OSError:
            pass

    throw_cmd = {}          # foe throw startup move -> (cmd, hml) this session
    last_throw_start = [None, None, None]   # (move, cmd, hml) of the last kind-16 seen
    escape = {"mv": None, "t": 0.0, "hp": 0, "tries": 0, "ok": 0, "by": {},
              "seq": None}      # {"st": start move, "opt": "6T", "hp0": hp, "t": t}
    try:
        with open(ESCAPES_FILE, encoding="utf-8") as fh:
            throw_esc = json.load(fh)
    except (OSError, ValueError):
        throw_esc = {}

    def pick_escape(ch, st_mv, cmd):
        """Same scoring as the throw answers: keep the guess for the
        CommandCode until it has failed twice, then the best (ok+1)/(n+2)."""
        st = throw_esc.setdefault(f"cmd{cmd or 0}", {})
        default = ESCAPE_GUESS.get(cmd, "T")
        d = st.get(default, [0, 0])
        if d[1] < 2 or (d[0] + 1) / (d[1] + 2) >= 0.5:
            return default
        best, best_s = default, (d[0] + 1) / (d[1] + 2)
        for opt in ESCAPE_OPTIONS:
            ok, n = st.get(opt, [0, 0])
            sc = (ok + 1) / (n + 2)
            if sc > best_s + 1e-9:
                best, best_s = opt, sc
        return best

    def press_escape(opt):
        names = []
        if opt == "6T":
            names = dirs_to_names(1 if facing_state[0] else -1, 0)
        elif opt == "4T":
            names = dirs_to_names(-1 if facing_state[0] else 1, 0)
        elif opt == "2T":
            names = ["down"]
        elif opt in ("3T", "1T"):
            # diagonal: horizontal a frame ahead, down WITH the button (the
            # recipe that landed 3K / 1P in the calibration)
            h = dirs_to_names((1 if opt == "3T" else -1) * (1 if facing_state[0] else -1), 0)
            inj.down(h); time.sleep(0.017)
            inj.down(["down", "throw"]); time.sleep(args.press)
            inj.up(["throw", "down"] + h)
            return
        if names:
            inj.down(names); time.sleep(0.008)
        inj.down(["throw"]); time.sleep(args.press)
        inj.up(["throw"] + names)

    wait_failed = {}        # throw start move -> times we stood for a T throw and the break failed
    far_whiff = {}          # throw start move -> consecutive whiffs seen from 140+ away
    far_note = [None]
    round_end = [0.0]       # when a health bar last hit 0: no pokes into the KO camera

    def finish_escape_seq():
        seq = escape["seq"]
        if seq is None:
            return
        escape["seq"] = None
        ok = me.get("CurrentHealth") >= seq["hp0"]
        if seq.get("hi") and not ok:
            ent = throw_ent(fchar, seq["st"], create=True)
            ent["hi"] = ent.get("hi", 0) + 1
            ent["dmg"] = ent.get("dmg", 0) + max(0, seq["hp0"] - me.get("CurrentHealth"))
            save_throw_dmg()
            if ent["hi"] == 2:
                print(f"  !    {seq['st']} has grabbed us out of our own attack twice: "
                      f"no more poking inside its reach (throw_damage.json)")
            # a hi-counter throw (it caught us inside our own attack: 8149
            # on our P after the run-up, 8284 on the 6P poke) cannot be
            # broken in DOA6 - the break input tells nothing here, and it
            # was booking those grabs against T and against "waiting"
            print(f"        ! throw {seq['st']} (cmd {seq['cmd']}) caught our attack "
                  f"(hi-counter): unbreakable, {seq['opt']} not scored")
            return
        if not ok:
            wait_failed[seq["st"]] = wait_failed.get(seq["st"], 0) + 1
            if wait_failed[seq["st"]] == 2:
                print(f"  !    {seq['st']}: standing for the break has cost us twice - "
                      f"answering it like a command throw from now on")
        st = throw_esc.setdefault(f"cmd{seq['cmd'] or 0}", {})
        st.setdefault(seq["opt"], [0, 0])
        st[seq["opt"]][0] += 1 if ok else 0
        st[seq["opt"]][1] += 1
        o, n = st[seq["opt"]]
        if ok:
            print(f"        ! throw {seq['st']} (cmd {seq['cmd']}) broken with {seq['opt']}  ({o}/{n})")
        else:
            nxt = pick_escape(fchar, seq["st"], seq["cmd"])
            print(f"        ! throw {seq['st']} (cmd {seq['cmd']}): {seq['opt']} did not break it "
                  f"({o}/{n})" + (f" - trying {nxt} next" if nxt != seq["opt"] else ""))
        try:
            with open(ESCAPES_FILE, "w", encoding="utf-8") as fh:
                json.dump(throw_esc, fh, indent=1, sort_keys=True)
        except OSError:
            pass
    prev_km = [None, None, 0]   # (kind, move, frame) last tick: a NEW throw start
                                # is a kind/move change OR the frame counter dropping
    throw_epoch = [0]       # +1 every time a throw startup begins
    throw_seen_t = [0.0]    # last tick the foe was in a throw startup (kind 16)
    answered_epoch = [-1]   # the epoch our last throw answer was for
    cornered = [0.0]        # until when we consider ourselves at a wall
    last_poke = [0.0]
    pokes = [0]
    poke_throttled = [False]
    hp_max = [0]            # our health at the top of this round
    careful = [False]       # under half health: pokes a third as often, no
                            # run-up attacks, no T first - the counter-hits
                            # on those are what finishes a low-health round

    def poke_gap_now():
        """4P landed 12/26, then 6/19, then 2/13 against the same CPU, and
        the misses cost 95 of 291 damage: it reads a poke that comes every
        0.45 s. Once the rate is under a quarter, poke a third as often."""
        ok, n = by_kind.get("poke", [0, 0])
        if careful[0]:
            return args.poke_gap * 3
        if n >= 10 and ok / n < 0.25:
            if not poke_throttled[0]:
                poke_throttled[0] = True
                print(f"  !    pokes landing {ok}/{n}: the CPU is reading them - "
                      f"poking a third as often from here")
            return args.poke_gap * 3
        return args.poke_gap
    dist_hist = []          # (t, distance) for the closing-in test
    wake_back = [0.0]       # last wake-up backdash
    prev_free = [True]      # were we free to act on the previous tick
    rounds = [0, 0]         # won, lost (a health bar reaching 0)
    prev_hp = [me.get("CurrentHealth"), foe.get("CurrentHealth")]
    last_hp = foe.get("CurrentHealth")

    def fire(kind, label, mv0, rem, pre=None):
        t = time.perf_counter()
        # bit 0 of GetAsyncKeyState = "pressed since the last call": tells
        # whether our J actually went through the OS input queue
        j_seen = bool(_user32.GetAsyncKeyState(_VK["j"]) & 1)
        pending.append({"t": t, "deadline": t + 1.2, "sample_at": t + 0.12,
                        "after": None, "kind": kind, "label": label,
                        "mv0": mv0, "rem": rem, "pre": pre or {},
                        "j_seen": j_seen, "trace": []})

    pre_stats = {}          # our move id before the input -> [landed, tried]

    def finish(e, ok):
        if e["kind"] not in ("jab", "punish", "approach", "poke"):
            k = e["pre"].get("mv")
            pre_stats.setdefault(k, [0, 0])
            pre_stats[k][1] += 1
            if ok:
                pre_stats[k][0] += 1
            if (e["pre"].get("mt") == MT_HIT and k is not None
                    and HIT_REACTION[0] <= k < HIT_REACTION[1]):
                st = stun_tab.setdefault(k, [0, 0])
                st[1] += 1
                if ok:
                    st[0] += 1
                # never came out = the game ignored the input in this stun;
                # a hold that whiffed (152-156) or caught did register
                came_out = ok or e["after"] != k
                ours = [r[0] for _, r in e["trace"]] + [e["after"]]
                fell = (not ok and any(m is not None and (125 <= m <= 135 or 70 <= m <= 79)
                                       for m in ours))
                if fell:
                    # 25121 -> 131: the "stun" was a fall, and the input
                    # became a wake-up. Nothing to hold there: retire it now
                    st[1] = max(st[1], 3)
                print(f"        {e['label']} in stun {k} frame {e['pre'].get('fr')}: "
                      + ("caught" if ok else ("a knockdown, not a stun" if fell else
                                              "came out, missed" if came_out
                                              else "input ignored"))
                      + f"  ({st[0]}/{st[1]} in this stun so far)")
                if stun_dead(k) and st[1] == 3:
                    print(f"  !    stun {k}: 0 for 3 - no more holds in it (stun_holds.json)")
                try:
                    with open(STUN_FILE, "w", encoding="utf-8") as fh:
                        json.dump({str(a): b for a, b in stun_tab.items()}, fh,
                                  indent=1, sort_keys=True)
                except OSError:
                    pass
        """Book one attempt. Our own move id ~120 ms after the input tells
        apart 'never came out' (still mv0), 'came out and whiffed' (a hold
        id, 152-156) and 'caught it' (anything else)."""
        nonlocal landed, no_reaction
        if e["kind"] == "break":
            ours = [r[0] for _, r in e["trace"]] + [e["after"]]
            walked_fwd = any(m in FWD_IDS for m in ours)
            raw, est = e.get("gauge", (None, None))
            if 8399 in ours and 8398 not in ours:
                # Fatal Rush = S alone: the game refused the Break Hold.
                # Nothing was spent, so refund; and unless the direction
                # was wrong (6S), our estimate was simply not enough.
                gauge_spent[0] = max(0, gauge_spent[0] - args.gauge_max // 2)
                if not walked_fwd and est is not None:
                    break_need[0] = max(break_need[0], est + 10)
                    print(f"        {e['label']} came out as Fatal Rush (8399): "
                          f"gauge est {est} was not enough - now need "
                          f"{break_need[0]}")
                else:
                    print(f"        {e['label']} came out as Fatal Rush (8399) "
                          f"with the direction mirrored (6S)")
            elif 8398 in ours and est is not None:
                print(f"        {e['label']} 4S accepted at gauge est {est} "
                      f"(raw {raw})")
        key = (e["kind"], ok)
        my_anim.setdefault(key, {})
        my_anim[key][e["after"]] = my_anim[key].get(e["after"], 0) + 1
        if ok:
            if e["kind"] not in ("jab", "punish", "approach", "poke"):
                landed += 1         # only holds count toward the hold rate
            by_kind.setdefault(e["kind"], [0, 0])[0] += 1
            if e["rem"] is not None:
                by_rem.setdefault(e["rem"], [0, 0])[0] += 1
        if e["kind"] == "low" and e["after"] is not None:
            wrong_hold[1] += 1
            # a catch animation (8xxx) on a low hold that did not land is the
            # mid hold having come out: the "down" was dropped
            if not ok and e["after"] >= 8000:
                wrong_hold[0] += 1
                if (not vert_lead[0] and wrong_hold[1] >= 15
                        and wrong_hold[0] / wrong_hold[1] > 0.3):
                    vert_lead[0] = True
                    print(f"  !    low holds caught with the wrong animation "
                          f"{wrong_hold[0]}/{wrong_hold[1]}: sending the hold's "
                          f"DOWN a frame before H from here")
        elif e["after"] == e["mv0"]:
            no_reaction += 1
            if e["kind"] == "poke" and by_kind.get("poke", [0, 0])[1] > 0:
                by_kind["poke"][1] -= 1     # never came out: not a read poke
            print(f"        {e['label']} no reaction from our character "
                  f"(move stayed {e['mv0']})")
        if (not ok and e["kind"] not in ("jab", "punish", "approach", "poke") and args.trace
                and e["after"] not in HOLD_IDS):
            p = e["pre"]
            print(f"        {e['label']} TRACE  before: me move={p.get('mv')} "
                  f"type={p.get('mt')} phase={p.get('ph')} | foe move={p.get('fmv')} "
                  f"phase={p.get('fph')} | J seen by OS: {e['j_seen']}")
            print("          frame: (me move, me type, me phase, foe move, foe phase)")
            for fr, row in e["trace"][:10]:
                print(f"          {fr:>5}: {row}")

    def settle(force=False):
        """One health drop credits exactly ONE attempt - the most recent -
        and retires the others. Crediting every pending entry made a jab's
        21 damage count as a landed hold too."""
        nonlocal last_hp
        now = time.perf_counter()
        my_mv = me.get("CurrentMove")
        # a "strike" that THROWS us out of our hold is an offensive hold
        # (char 31's 8009: three midp holds ended in our MoveType 5, 103 dmg).
        # Holds and guards both lose to it; remember it and step instead.
        if (pending and me.get("MoveType") == MT_THROWN
                and pending[-1]["kind"] in ("high", "midp", "midk", "low", "break")
                and now - pending[-1]["t"] < 0.35
                and now - throw_seen_t[0] > 0.5):
            # ...and no plain throw (kind 16 startup) in the last half
            # second: a hold that whiffs on P gets thrown by the CPU's 6-frame
            # T right after, and that put P (176), the low kicks (209/210)
            # and half of Nyotengu's move list into oh.json as "offensive
            # holds" - every one of them then guarded instead of held
            e = pending[-1]
            fmv_oh = e["pre"].get("fmv")
            ch = str(foe.get("CurrentCharacter"))
            if fmv_oh and fmv_oh not in oh_seen.setdefault(ch, set()):
                oh_seen[ch].add(fmv_oh)
                oh_stats[1] += 1
                print(f"  !    {fmv_oh} is an OFFENSIVE HOLD: our {e['kind']} hold got "
                      f"thrown by it. Sidestepping it from now on (oh.json)")
                try:
                    with open(OH_FILE, "w", encoding="utf-8") as fh:
                        json.dump({k: sorted(v) for k, v in oh_seen.items()}, fh, indent=1)
                except OSError:
                    pass
        for e in pending:
            if e["after"] is None and now >= e["sample_at"]:
                e["after"] = my_mv
            if now - e["t"] < 0.6:
                row = (my_mv, me.get("MoveType"), me.get("Phase"),
                       foe.get("CurrentMove"), foe.get("Phase"))
                if not e["trace"] or e["trace"][-1][1] != row:
                    e["trace"].append((round((now - e["t"]) * 60, 1), row))
                    # The first walk id in a hold's trace tells two things the
                    # idle probe often cannot (the CPU never stands still):
                    # how long a key takes to show (horizontal press -> id),
                    # and whether our facing guess was mirrored. Learn both
                    # here, live, so the NEXT hold is right even under lag.
                    if ("dx" in e and not e.get("walk_seen") and not e.get("lag_known")
                            and my_mv in FWD_IDS + BACK_IDS):
                        e["walk_seen"] = True
                        lag = (e.get("o_wait", 0.0) + (now - e["t"])) * 1000
                        if 20 < lag < 300:
                            input_lag_hist.append(lag)
                            if len(input_lag_hist) > 15:
                                del input_lag_hist[0]
                            med = sorted(input_lag_hist)[len(input_lag_hist) // 2]
                            if len(input_lag_hist) >= 5 and (
                                    not lag_reported[0] or abs(med - input_lag[0]) > 10):
                                lag_reported[0] = True
                                print(f"  input lag: ~{med:.0f} ms from key to game "
                                      f"({med / (1000 / 60):.1f} frames; 42 in the good "
                                      f"sessions)" + ("  - HIGH: holds go direction+H "
                                      "together, facing from the probes" if med > 80 else ""))
                            if len(input_lag_hist) >= 3:
                                input_lag[0] = med
                        if not e.get("flipped"):
                            mirrored = ((my_mv in FWD_IDS) if e["dx"] < 0
                                        else (my_mv in BACK_IDS))
                            if mirrored:
                                facing_state[0] = not facing_state[0]
                                facing_fixes[0] += 1
                                print(f"        {e['label']} walked the wrong way (id "
                                      f"{my_mv}): facing flipped for the next one")
        hp = foe.get("CurrentHealth")
        if pending and me.get("MoveType") == MT_HOLD_HIT and \
                pending[-1]["kind"] != "jab":
            e = pending[-1]
            if e["after"] is None:
                e["after"] = my_mv
            print(f"        {e['label']} CAUGHT (our MoveType 6, move {my_mv})")
            last_hold_caught[0] = now
            facing_dirty[0] = True              # our hold throws them past us
            hold_watch[0] = {"kind": e["kind"], "hp0": hp, "t": now,
                             "fmv": e["pre"].get("fmv")}
            finish(e, True)
            for other in pending[:-1]:
                if other["after"] is None:
                    other["after"] = my_mv
                finish(other, False)
            pending.clear()
            last_hp = hp
            return
        if hp < last_hp and pending:
            e = pending[-1]
            if e["after"] is None:
                e["after"] = my_mv
            print(f"        {e['label']} LANDED ({last_hp - hp} dmg)")
            finish(e, True)
            for other in pending[:-1]:
                if other["after"] is None:
                    other["after"] = my_mv
                finish(other, False)
            pending.clear()
        last_hp = hp
        keep = []
        for e in pending:
            if force or now >= e["deadline"]:
                if e["after"] is None:
                    e["after"] = my_mv
                finish(e, False)
            else:
                keep.append(e)
        pending[:] = keep

    def side_check():
        """Tap right; the character that starts walking is ours. Needs only
        one of the two to be idle (the earlier version needed both, and a
        CPU that never stands still kept us inverted for a whole match)."""
        nonlocal me, foe, me_side, foe_side
        me.refresh(); foe.refresh()
        me_idle = me.get("CurrentMove") == 0 and me.get("MoveKind") == 0
        foe_idle = foe.get("CurrentMove") == 0 and foe.get("MoveKind") == 0
        if not (me_idle or foe_idle):
            return
        inj.down(["right"]); time.sleep(0.018); inj.up(["right"])
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.2:
            me.refresh(); foe.refresh()
            if me_idle and me.get("CurrentMove") in FWD_IDS + BACK_IDS:
                side_confirmed[0] = True
                print(f"  side: confirmed - we are {me_side}")
                return
            if foe_idle and foe.get("MoveKind") == 0                     and foe.get("CurrentMove") in FWD_IDS + BACK_IDS:
                side_votes[0] += 1
                if side_votes[0] >= 2:
                    me, foe = foe, me
                    me_side, foe_side = foe_side, me_side
                    pos_mem[0], pos_mem[1] = pos_mem[1], pos_mem[0]
                    side_confirmed[0] = True
                    facing_state[0] = not facing_state[0]
                    print(f"  side: the keyboard moved the {me_side} character - we are "
                          f"{me_side}, not {foe_side}. Readers swapped.")
                return
            time.sleep(0.003)

    side_next = [0.0]
    if not side_confirmed[0] and not args.dry_run:
        side_before = me_side
        for _ in range(8):                  # ~3 s: one of them idle -> tap right
            me.refresh(); foe.refresh()
            if me.get("CurrentHealth") > 0 and foe.get("CurrentHealth") > 0:
                side_check()
                if side_confirmed[0]:
                    break
            time.sleep(0.35)
        if not side_confirmed[0]:
            print("  side: not confirmed yet (assuming P1); the idle probe keeps checking")
        elif me_side != side_before:
            my_char = me.get("CurrentCharacter")
            if poke_auto:
                args.poke = CHAR_POKE.get(my_char, "punch")
            if combo_auto:
                combo_table = dict(CHAR_COMBOS.get(my_char, {"default": GENERIC_COMBO}))
                combo_seqs = {k: parse_combo(v) for k, v in combo_table.items()}
                combo_seqs = {k: v for k, v in combo_seqs.items() if v}
            who = CHAR_NAMES.get(my_char, f"char {my_char}")
            print(f"playing {me_side} (char {my_char}) after the side check; offence: "
                  f"poke = {args.poke}; combos for {who}: "
                  + "; ".join(f"{k}: {combo_table[k]}" for k in combo_seqs))

    anchor_check = [0.0]
    # Game frame time, measured from the move-frame counters. Every wall-clock
    # wait in here was tuned at 60 fps (a walk id shows ~42 ms after a key):
    # in one match the ids took ~90 ms, 31 of 32 facing probes timed out and
    # the holds came out mirrored (31%). Scale the waits by the real frame time.
    frame_ms = [1000 / 60.0]
    base_press = args.press
    _ft = {"last": (None, None), "t": 0.0, "deltas": []}

    def sample_frame_time():
        cur = (me.get("CurrentMoveFrame"), foe.get("CurrentMoveFrame"))
        t = time.perf_counter()
        if cur != _ft["last"]:
            if _ft["t"]:
                d = (t - _ft["t"]) * 1000
                if 6 < d < 120:
                    _ft["deltas"].append(d)
                    if len(_ft["deltas"]) > 120:
                        del _ft["deltas"][:60]
            _ft["last"], _ft["t"] = cur, t
        if len(_ft["deltas"]) >= 30:
            ds = sorted(_ft["deltas"])
            fm = ds[len(ds) // 2]
            if abs(fm - frame_ms[0]) > 3:
                print(f"  game frame time ~{fm:.1f} ms ({1000 / fm:.0f} fps): "
                      f"input waits rescaled")
            frame_ms[0] = fm
            args.press = max(base_press, 1.3 * fm / 1000)

    anchor_reject = [None]

    def refresh_anchors():
        """Survival / arcade: the next opponent is a NEW object, and often
        ours is re-created too. Walk the static pointer chains again; if
        they land elsewhere, move both Side readers and re-attach the
        position fields at the remembered offsets."""
        try:
            new_an = locate_all(proc, layout)
        except Exception:
            return
        if not new_an.get("state") or not new_an.get("state:P2"):
            return
        moved = [k for k in ("state", "state:P2", "pos") if new_an.get(k) != anchors.get(k)]
        if not moved:
            return
        # the chain once landed inside the exe image (0x7FF6...): char
        # 32758, hp 59936, move 46176, kind 116 - and the bot stood there
        # for a round being hit by a ghost. A fighter object has a small
        # character id, a health bar and a small move kind: anything else
        # is not a fighter, keep the anchors we have and look again later
        try:
            for side_name in ("P1", "P2"):
                ch = read_field(proc, new_an, f"{side_name}_CurrentCharacter", layout)
                hp = read_field(proc, new_an, f"{side_name}_CurrentHealth", layout)
                mk = read_field(proc, new_an, f"{side_name}_MoveKind", layout)
                if not (0 <= ch < 128 and 0 <= hp <= 2000 and 0 <= mk < 64):
                    if anchor_reject[0] != new_an.get("state:P2"):
                        anchor_reject[0] = new_an.get("state:P2")
                        print(f"  [!] new anchors rejected: {side_name} reads char {ch} "
                              f"hp {hp} kind {mk} at 0x{new_an.get('state:P2', 0):X} - "
                              f"not a fighter object, keeping the old ones")
                    return
        except Exception:
            return
        anchors.update(new_an)
        for side_obj, side_name in ((me, me_side), (foe, foe_side)):
            side_obj.base = (anchors.get(f"state:{side_name}", anchors["state"])
                             + layout["players"][side_name]["state"])
        facing_dirty[0] = True                  # a new opponent, either side
        print(f"anchors moved ({', '.join(moved)}): new objects at "
              + "  ".join(f"{k}=0x{anchors[k]:X}" for k in ("state", "state:P2")))
        if pos_mem[0] is not None:
            me.pos, foe.pos = me.base + pos_mem[0], foe.base + pos_mem[1]
        pos_ok[0] = False
        pos_ok[1] = time.perf_counter() - 0.5      # re-verify within half a second

    try:
        while True:
            now = time.perf_counter()
            if now - anchor_check[0] > 1.0:
                anchor_check[0] = now
                refresh_anchors()
            if not close_throw["done"]:
                # both sides read type 5 during a throw: only THEIR type 5
                # while we are not being thrown, or their health dropping,
                # means our T connected (one 8137 grab was booked as a success)
                if (foe.get("CurrentHealth") < close_throw["hp0"]
                        or (foe.get("MoveType") == MT_THROWN
                            and me.get("MoveType") != MT_THROWN)):
                    ct_resolve(True)
                elif (me.get("CurrentHealth") < close_throw["myhp0"]
                      or now - close_throw["t"] > 0.8):
                    ct_resolve(False)
            if not last_answer["done"] and now - last_answer["t"] > 1.5:
                record_answer(True)
            if not side_confirmed[0] and not args.dry_run and now - side_next[0] > 1.0:
                side_next[0] = now
                side_check()
                if side_confirmed[0] and me.get("CurrentCharacter") != my_char:
                    my_char = me.get("CurrentCharacter")
                    if poke_auto:
                        args.poke = CHAR_POKE.get(my_char, "punch")
                    if combo_auto:
                        combo_table = dict(CHAR_COMBOS.get(my_char, {"default": GENERIC_COMBO}))
                        combo_seqs = {k: parse_combo(v) for k, v in combo_table.items()}
                        combo_seqs = {k: v for k, v in combo_seqs.items() if v}
                    print(f"playing {me_side} (char {my_char}) after the side check; "
                          f"offence: poke = {args.poke}; combos: "
                          + "; ".join(f"{k}: {combo_table[k]}" for k in combo_seqs))
            foe.refresh()
            me.refresh()
            sample_frame_time()
            settle()

            row = (foe.get("CurrentMove"), foe.get("MoveKind"), foe.get("Phase"),
                   me.get("CurrentMove"), me.get("MoveType"),
                   me.get("CurrentHealth"), foe.get("CurrentHealth"))
            if not history or history[-1][1:] != row:
                history.append((now,) + row)
                if len(history) > 400:
                    del history[:100]

            for side_name, side in (("me", me), ("foe", foe)):
                gv = side.get("BreakMeter")
                gauge_seen[side_name][0] = min(gauge_seen[side_name][0], gv)
                gauge_seen[side_name][1] = max(gauge_seen[side_name][1], gv)
            my_hp_now = me.get("CurrentHealth")
            foe_hp_now = foe.get("CurrentHealth")
            # a KO leaves the winner with health: both bars at 0 is the
            # object reset between matches (arcade: 7 "round lost, their hp
            # 0" in a 17-0 session, each right after "anchors moved")
            if foe_hp_now == 0 and prev_hp[1] > 0 and my_hp_now > 0:
                rounds[0] += 1
                round_end[0] = now
                print(f"  ===== ROUND WON  ({rounds[0]}-{rounds[1]}) our hp {my_hp_now}")
            if my_hp_now == 0 and prev_hp[0] > 0 and foe_hp_now > 0:
                rounds[1] += 1
                round_end[0] = now
                print(f"  ===== round lost ({rounds[0]}-{rounds[1]}) their hp {foe_hp_now}")
            if 0 < my_hp_now <= 2000:
                if my_hp_now > hp_max[0] or my_hp_now > prev_hp[0] + 50:
                    hp_max[0] = my_hp_now          # a new round refilled the bar
                was_careful = careful[0]
                careful[0] = hp_max[0] > 0 and my_hp_now < 0.5 * hp_max[0]
                if careful[0] and not was_careful:
                    print(f"  hp {my_hp_now}/{hp_max[0]}: careful mode - pokes a third as "
                          f"often, no run-up attacks, no T first")
                elif was_careful and not careful[0]:
                    print(f"  hp {my_hp_now}/{hp_max[0]}: back to normal offence")
            prev_hp[:] = [my_hp_now, foe_hp_now]
            if my_hp_now < last_my_hp and me.get("MoveType") in (0, 10, 13, 17):
                pass                                # round end / reset, not a hit
            elif my_hp_now < last_my_hp and last_my_hp - my_hp_now > 500:
                pass                # a stale object between anchor moves read
                                    # 43394 (twice in one run): not a hit
            elif my_hp_now < last_my_hp:
                d = last_my_hp - my_hp_now
                fmv = foe.get("CurrentMove")
                act = last_action[0] if now - last_action[1] < 0.8 else "nothing"
                thrown = (me.get("MoveType") == MT_THROWN
                          or foe.get("MoveKind") in (4, MK_THROW))
                if thrown:
                    act += "/thrown"
                dmg_by_move[fmv] = dmg_by_move.get(fmv, 0) + d
                if not last_answer["done"] and (
                        (last_answer["mv"] == fmv and now - last_answer["t"] < 1.5)
                        or now - last_answer["t"] < 0.8):
                    # 8009 hit us out of the crouch. The hit often lands
                    # under a later id of the same move (char 31's OH 8021
                    # hit a croucher 10 times and was booked 10/10 "ok"
                    # because the damage read as another id): any damage
                    # within 0.8 s of the answer means the answer failed
                    record_answer(False, d)
                dmg_by_state[act] = dmg_by_state.get(act, 0) + d
                my_mv_hit = me.get("CurrentMove")
                if 125 <= my_mv_hit <= 135 or 70 <= my_mv_hit <= 79:
                    print(f"        HIT ON THE GROUND for {d} by move {fmv} "
                          f"(our state {my_mv_hit}, type {me.get('MoveType')})")
                if (thrown and throw_dumps[0] < 6 and args.trace
                        and (fmv != throw_dumps[1] or now - throw_dumps[2] > 3.0)):
                    throw_dumps[0] += 1
                    throw_dumps[1:] = [fmv, now]
                    print(f"        THROWN for {d} by move {fmv} - last state changes "
                          f"(frames ago: foe move/kind/phase | me move/type | hp me/foe):")
                    for (t, a, b, c, dd, ee, h1, h2) in history[-20:]:
                        print(f"          -{(now - t) * 60:6.1f}: foe {a:<6} k{b:<3} p{c} "
                              f"| me {dd:<6} t{ee} | {h1}/{h2}")
            last_my_hp = my_hp_now

            mt = foe.get("MoveType")
            mv = foe.get("CurrentMove")
            ph = foe.get("Phase")
            fr = foe.get("CurrentMoveFrame")
            fchar = foe.get("CurrentCharacter")
            kind = foe.get("MoveKind")
            if kind == MK_THROW:
                throw_seen_t[0] = now
            if kind == MK_THROW and (prev_km[0] != MK_THROW or prev_km[1] != mv
                                     or fr < prev_km[2]):
                throw_epoch[0] += 1
            prev_km[:] = [kind, mv, fr]

            # Learn startup from the phase flip, whatever else happens.
            #
            # The move id is NOT stable through a strike: the CPU's 8152
            # became 8153 by the time it went active, 176 became 8041, and so
            # on - the id read at frame 1 (the one the bot has to decide on)
            # is often not the id at the flip. So track the whole attack
            # episode from its first startup frame, and when the flip comes
            # record the startup under the id the episode STARTED with, using
            # frames counted from the episode start (the counter may restart
            # with the id change), and under the current id as well.
            LEARN_KINDS = MK_STRIKES + (MK_THROW,)
            if kind in LEARN_KINDS and ph == PH_STARTUP:
                # An id change mid-startup usually restarts the counter at
                # 1, so "new id at frame 1" is NOT a new attack. An episode
                # ends only when the strike goes active or stops being a
                # strike; anything longer than 90 frames is discarded.
                if episode is not None and (episode["base"] + fr > 90
                                            or episode["kind"] != kind):
                    episode = None
                if episode is None:
                    episode = {"mv0": mv, "mv_last": mv, "base": 0,
                               "f_last": fr, "kind": kind}
                else:
                    if fr < episode["f_last"]:          # counter restarted
                        episode["base"] += episode["f_last"]
                    episode["f_last"] = fr
                    episode["mv_last"] = mv
            elif kind in LEARN_KINDS and ph == PH_ACTIVE and episode is not None \
                    and last_phase == PH_STARTUP and episode["kind"] == kind:
                total = episode["base"] + fr
                if fr < episode["f_last"]:
                    total = episode["base"] + episode["f_last"] + fr
                pairs = ((episode["mv0"], total), (mv, fr))
                if kind == MK_THROW:
                    pairs = ((mv, fr),)      # a throw's id does not alias
                for key, val in pairs:
                    if startup.learn(fchar, key, val):
                        learned_now += 1
                        print(f"  learned: char {fchar} move {key} active at "
                              f"frame {val}"
                              + (f"  (started as {episode['mv0']})"
                                 if key != episode["mv0"] else ""))
                episode = None
            elif kind not in LEARN_KINDS:
                episode = None
            last_phase, last_move = ph, mv

            # ---- our guard is up: decide every tick whether to keep it.
            # The old guard() slept for the whole duration, and that sleep is
            # where the CPU turned its run-up into a throw unseen. Now Free
            # stays down only while it is still the right answer.
            if guarding is not None:
                g = guarding
                release = None
                if kind == MK_THROW:
                    release = "throw"              # throws beat guard: move
                elif kind in MK_STRIKES and ph == PH_RECOVERY and mv != 0:
                    release = "recovery"
                elif kind == 0 and mv == 0:
                    release = "idle"
                elif now > g["until"]:
                    release = "timeout"
                elif (kind in MK_STRIKES and ph == PH_STARTUP and mv != g["mv"]
                        and startup.get(fchar, mv) is not None):
                    release = "newstrike"          # something holdable instead
                elif (g["known"] is None and kind in MK_STRIKES
                        and ph == PH_STARTUP and fr >= 32):
                    release = "runup"              # 32 frames and never active
                if release is None:
                    time.sleep(period)
                    continue
                inj.up(g["names"] + ["free"])
                guarding = None
                armed = True
                last_fire = 0.0                    # free to act at once
                if release == "runup":
                    startup.seen[(fchar, mv)] = max(startup.seen.get((fchar, mv), 0), 3)
                    startup.dirty = True
                    unknown_seen[mv] = 3
                    print(f"        ={g['idx']} guard released: {mv} is a run-up "
                          f"({fr} frames, never active)")
                elif release == "recovery" and args.punish_after_guard != "none":
                    # they are in recovery; wait (briefly) until we are free
                    t_end = time.perf_counter() + 0.25
                    while time.perf_counter() < t_end:
                        me.refresh(); foe.refresh()
                        if foe.get("Phase") != PH_RECOVERY or foe.get("MoveKind") not in MK_STRIKES:
                            break
                        if me.get("MoveType") in (MT_IDLE, MT_STRIKE) and \
                                me.get("CurrentMove") in (0, WALK_FWD, WALK_BACK):
                            left = foe.get("AnimLength") - foe.get("CurrentMoveFrame")
                            if left >= args.punish_min_recovery:
                                d2, _ = distance()
                                btn = args.punish_after_guard
                                if btn == "auto":
                                    btn = ("throw" if d2 <= args.punish_throw_range else
                                           "punch" if d2 <= args.punish_punch_range
                                           else None)
                                if (btn and args.break_blow
                                        and gauge_now() >= args.gauge_max
                                        and d2 <= args.punish_punch_range):
                                    # 6S: forward + Special, the Break Blow. A
                                    # mirrored forward is 4S = Break Hold (8398,
                                    # both attempts one match): with 40+ frames
                                    # of recovery to spend, probe the facing
                                    # first - hold forward, read the walk id.
                                    fwd = dirs_to_names(1 if facing_state[0] else -1, 0)
                                    inj.down(fwd)
                                    t_f = time.perf_counter()
                                    while time.perf_counter() - t_f < min(0.15, input_lag[0] / 1000 + 0.03):
                                        me.refresh()
                                        w = me.get("CurrentMove")
                                        if w in FWD_IDS:
                                            break
                                        if w in BACK_IDS:              # mirrored
                                            inj.up(fwd)
                                            facing_state[0] = not facing_state[0]
                                            facing_fixes[0] += 1
                                            fwd = dirs_to_names(1 if facing_state[0] else -1, 0)
                                            inj.down(fwd)
                                            time.sleep(0.02)
                                            break
                                        time.sleep(0.002)
                                    inj.down(["special"])
                                    time.sleep(args.press)
                                    inj.up(["special"]); inj.up(fwd)
                                    breaks[1] += 1
                                    gauge_spent[0] += args.gauge_max
                                    btn = "BREAK BLOW"
                                    punished += 1
                                    by_kind.setdefault("punish", [0, 0])[1] += 1
                                    fire("punish", f"={g['idx']} break blow",
                                         me.get("CurrentMove"), None, None)
                                    last_action[:] = ["punish", time.perf_counter()]
                                    last_fire = time.perf_counter()
                                    print(f"        ={g['idx']} guard released -> "
                                          f"BREAK BLOW @{d2:.0f}")
                                    btn = None
                                if btn:
                                    pre = {"mv": me.get("CurrentMove"),
                                           "mt": me.get("MoveType"), "ph": 0,
                                           "fmv": foe.get("CurrentMove"),
                                           "fph": foe.get("Phase")}
                                    jab(inj, btn, press=args.press)
                                    punished += 1
                                    by_kind.setdefault("punish", [0, 0])[1] += 1
                                    fire("punish", f"={g['idx']} punish",
                                         me.get("CurrentMove"), None, pre)
                                    last_action[:] = ["punish", time.perf_counter()]
                                    last_fire = time.perf_counter()
                                    print(f"        ={g['idx']} guard released -> "
                                          f"{btn} @{d2:.0f} ({left} frames left)")
                            break
                        time.sleep(0.003)
                    time.sleep(period)
                    continue
                elif release == "throw":
                    pass                           # handled just below
                else:
                    time.sleep(period)
                    continue

            if kind in (MK_THROW, 4):
                last_throw_seen[0] = now
            incoming_strike = (kind in MK_STRIKES and ph == PH_STARTUP
                               and mv != 0)
            incoming_throw = (args.punish_throws and kind == MK_THROW
                              and ph == PH_STARTUP)
            my_type_now = me.get("MoveType")
            my_mv_tick = me.get("CurrentMove")
            # ---- on the ground (ids 125-135, idle type): the CPU pokes a
            # lying body with lows for as long as we lie there. H rises.
            if 125 <= my_mv_tick <= 135 and my_type_now == MT_IDLE:
                if rise_tap[0] != my_mv_tick:
                    rise_tap[:] = [my_mv_tick, now, 0]
                    if not args.dry_run:            # at once: the tech-roll window
                        inj.down(["free"]); time.sleep(0.030); inj.up(["free"])
                elif (len(rise_tap) > 2 and rise_tap[2] < 1 and rise_tap[1]
                        and now - rise_tap[1] > 0.15 and not args.dry_run):
                    rise_tap[2] = 1
                    inj.down(["free"]); time.sleep(0.030); inj.up(["free"])
                    print(f"  ^    down (id {my_mv_tick}): tapped H to rise (x2)")
                    facing_dirty[0] = True
            elif rise_tap[0] is not None and not (125 <= my_mv_tick <= 135):
                rise_tap[:] = [None, 0.0]
            # ---- gauge hunt: +0x584 is not the bar. Whichever side does a
            # Break Hold (8398), diff its block 0.3 s before vs 0.5 s after
            # and list the u16 fields that went DOWN - the real gauge must.
            hunt_ring.append((now, me.buf, foe.buf))
            if len(hunt_ring) > 80:
                del hunt_ring[:20]
            if hunt is None:
                for sname, sobj, idx in (("P1/me", me, 1), ("P2/foe", foe, 2)):
                    if sobj.get("CurrentMove") == 8398:
                        before = None
                        for t_r, b1, b2 in hunt_ring:
                            if now - t_r >= 0.30:
                                before = (b1, b2)[idx - 1]
                        if before is not None:
                            hunt = {"side": sname, "idx": idx, "t": now, "before": before}
                        break
            elif now - hunt["t"] > 0.50:
                after = (me.buf, foe.buf)[hunt["idx"] - 1]
                drops = []
                for off in range(0, min(len(after), len(hunt["before"])) - 1, 2):
                    b = struct.unpack_from("<H", hunt["before"], off)[0]
                    a = struct.unpack_from("<H", after, off)[0]
                    if 20 <= b <= 2000 and a < b:
                        drops.append(f"+0x{off:X}: {b}->{a}")
                print(f"  gauge hunt ({hunt['side']} Break Hold): u16 fields that "
                      f"dropped: {', '.join(drops[:16]) or 'none'}")
                hunt = None
            # ---- how much did that hold actually take? (the whole hold
            # animation, until our MoveType leaves 6)
            hw = hold_watch[0]
            if hw and ((my_type_now != MT_HOLD_HIT and now - hw["t"] > 0.3)
                       or now - hw["t"] > 4.0):
                dealt = max(0, hw["hp0"] - foe.get("CurrentHealth"))
                hold_dmg.setdefault(hw["kind"], [0, 0])
                hold_dmg[hw["kind"]][0] += dealt
                hold_dmg[hw["kind"]][1] += 1
                print(f"        hold damage: {hw['kind']} took {dealt}")
                if dealt == 0 and hw.get("fmv") and hw["kind"] in ("high", "midp", "midk", "low"):
                    ch = str(fchar)
                    cnt = nohold_cnt.setdefault(ch, {})
                    cnt[hw["fmv"]] = cnt.get(hw["fmv"], 0) + 1
                    if cnt[hw["fmv"]] >= 2 and hw["fmv"] not in nohold_seen.setdefault(ch, set()):
                        nohold_seen[ch].add(hw["fmv"])
                        print(f"  !    {hw['fmv']}: two holds 'caught' it for 0 damage - "
                              f"not holdable. Guarding it from now on (nohold.json)")
                        try:
                            with open(NOHOLD_FILE, "w", encoding="utf-8") as fh:
                                json.dump({k: sorted(v) for k, v in nohold_seen.items()}, fh, indent=1)
                        except OSError:
                            pass
                hold_watch[0] = None
            # ---- one of our strikes connected (foe kind 8 = hit stun), they
            # are airborne (kind 9), or they lie down after our hold (kind 12):
            # run the character's string.
            #
            # Timing, learned the hard way: an input during our move's ACTIVE
            # phase is dropped unless it is a string branch (PP -> PPP took it;
            # S after PPP and 3P after P+K did not, and we stood idle while
            # they fell). So: press when our move is in RECOVERY (phase 2) -
            # the game buffers it to the first free frame - and if we still
            # end up idle with the input not taken, press it again at once.
            if combo_seqs and not args.dry_run and foe.get("CurrentHealth") == 0                     and combo["hp0"] is not None:
                combo_reset("round over")          # no S spam into the KO freeze
            if combo_seqs and not args.dry_run and foe.get("CurrentHealth") > 0:
                foe_open = kind in (8, 9)
                foe_down = kind == 12
                my_kind = me.get("MoveKind")
                in_strike = (my_type_now == MT_STRIKE and my_kind in MK_STRIKES
                             and my_mv_tick not in (0, WALK_FWD, WALK_BACK))
                # MoveKind 0 is neutral: standing, walking, and for Minato the
                # dance she plays on the spot (ids like 8022/8027). "move id 0"
                # is never true for her, so she read as never idle.
                my_idle = my_type_now == MT_IDLE and my_kind == 0
                if combo["hp0"] is None:
                    if combo.get("skip_mv") is not None and my_mv_tick != combo["skip_mv"]:
                        combo["skip_mv"] = None        # we moved on: openers count again
                    if foe_open and in_strike and combo.get("skip_mv") is None:
                        # (an "air" opener from idle - they are airborne, we
                        # stand - is gone: a juggle has to be buffered in the
                        # launcher's recovery; pressed from idle it whiffed
                        # under the falling body 20+ times, 2 hits of 8-14)
                        rkey = my_mv_tick if (in_strike and my_mv_tick in combo_seqs) else "default"
                        if (last_action[0] == "lowkick" and now - last_action[1] < 0.9
                                and "lowkick" in RECIPE_POOL.get(my_char, {})):
                            rkey = "lowkick"      # the throw-answer 2K hit: its own pool
                        recipe, seq = choose_recipe(rkey)
                        if seq is None:
                            seq = (combo_seqs.get(my_mv_tick, combo_seqs.get("default"))
                                   if in_strike else combo_seqs.get("default"))
                        combo["recipe"], combo["rkey"] = recipe, rkey
                        # (a 6S on the stun was tried and dropped: a non-string
                        # input is only taken once we are idle, and the Break
                        # Blow's long startup then whiffed on a recovered foe -
                        # 8381 for 0 dmg, or a plain S. The S string spends the
                        # full bar by itself: its 4th S IS the Break Blow.)
                        if seq:
                            combo.update(hp0=foe.get("CurrentHealth"), t=now,
                                         last_mv=None, seq=seq, await_=False, again=0,
                                         opener=my_mv_tick if in_strike else "air",
                                         myhp0=me.get("CurrentHealth"))
                            combo_stats["started"] += 1
                    elif (foe_down and my_idle and combo_seqs.get("ground")
                          and now - last_hold_caught[0] < 3.0):
                        combo.update(hp0=foe.get("CurrentHealth"), t=now, last_mv=None,
                                     seq=combo_seqs["ground"], opener="hold", await_=False, again=0)
                        combo_stats["started"] += 1
                if combo["hp0"] is not None:
                    cseq = combo["seq"]
                    d_c, _ = distance()
                    # was the last input taken? our move id left the one we
                    # pressed it during
                    if (combo.get("await_") and my_kind != 0
                            and my_mv_tick not in (
                                0, combo["last_mv"], WALK_FWD, WALK_BACK)):
                        # kind 0 would be the dance, not the move we pressed:
                        # "3PK came out as our move 8023" was her idle, and the
                        # string moved on from an input that never landed
                        combo["await_"] = False
                        combo["again"] = 0
                        print(f"        + {combo.get('tok', '?')} came out as our move "
                              f"{my_mv_tick}")
                    nxt = cseq[combo["i"]] if combo["i"] < len(cseq) else None
                    nxt_is_throw = nxt is not None and nxt[1] == "throw"
                    # MoveKind 13 is a stance (Minato's Shuffle, entered by
                    # the bare "4" in P,P,P,4,6P). Nothing of ours is playing
                    # there, so the stance move is ready for the next input
                    # even though its MoveType is not the striking one.
                    in_stance = my_kind == 13
                    ready = (in_stance or
                             (my_type_now == MT_STRIKE
                              and my_mv_tick not in (0, WALK_FWD, WALK_BACK)
                              and my_mv_tick != combo["last_mv"]
                              and (my_kind not in MK_STRIKES
                                   or me.get("Phase") >= PH_RECOVERY)))
                    do_press = repress = False
                    waiting_ok = (my_idle or in_stance or (my_type_now == MT_STRIKE
                                              and my_mv_tick == combo["last_mv"]
                                              and me.get("Phase") >= PH_RECOVERY))
                    if combo.get("await_") and waiting_ok and (foe_open or nxt_is_throw) \
                            and now - combo["t"] > 0.04 and d_c <= args.combo_range:
                        # not taken yet: the buffer window is the tail of the
                        # recovery, so keep re-pressing every ~2 frames until
                        # our move id changes. Give up by TIME, not count: a
                        # launcher's recovery (8K, 8144) outlasted 6 presses
                        # and the juggle 6P was abandoned before we were even
                        # idle - 0.5 s after we are idle, or 1.2 s in all
                        t_first = combo.get("t_first") or combo["t"]
                        if (now - t_first > 1.2) or (my_idle and now - t_first > 0.5):
                            combo_reset(f"{combo.get('tok', '?')} not accepted "
                                        f"({combo.get('again', 0) + 1}x, {now - t_first:.2f}s)")
                        else:
                            combo["again"] = combo.get("again", 0) + 1
                            combo["i"] -= 1
                            nxt = cseq[combo["i"]]
                            do_press = repress = True
                    elif nxt is None:
                        if not in_strike and now - combo["t"] > 0.35:
                            combo_reset("string done")
                    elif nxt_is_throw:
                        # 2T only on a body on the floor, from standing
                        if foe_down and my_idle and d_c <= 130 and now - combo["t"] > 0.20:
                            do_press = True
                        elif now - combo["t"] > 1.2 or (kind == 0 and now - combo["t"] > 0.3):
                            combo_reset("no ground-throw window")
                    elif not foe_open and not in_stance and now - combo["t"] > 0.25:
                        combo_reset("they recovered" if kind == 0 else f"foe kind {kind}")
                    elif ready and nxt is not None and d_c > step_reach(nxt):
                        # P+K knocked them to 178: the PP after it can only
                        # whiff, and the whiff is what the CPU throws
                        combo_reset(f"{nxt[2]} out of reach ({d_c:.0f})")
                    elif ready or (foe_open and my_idle and d_c <= args.combo_range
                                   and now - combo["t"] > 0.03):
                        do_press = True
                    elif (my_type_now in (MT_HIT, MT_THROWN, MT_HOLD_HIT, 7)
                          and not in_stance and now - combo["t"] > 0.1):
                        combo_reset(f"we are type {my_type_now}")   # hit / thrown / held
                    if do_press:
                        if zoning is not None:
                            inj.up(zoning); zoning = None
                        in_string[0] = combo["i"] > 0
                        tok = combo_press(nxt)
                        in_string[0] = False
                        combo["i"] += 1
                        combo["last_mv"] = my_mv_tick
                        combo["t"] = now
                        if not repress:
                            combo["t_first"] = now
                        combo["await_"] = True
                        combo["tok"] = tok
                        if not repress:
                            combo["hits"] += 1
                            combo_stats["hits"] += 1
                        last_fire = now
                        last_action[:] = ["combo", now]
                        if not repress or combo.get("again", 0) in (1, 5, 10, 20):
                            print(f"  +    combo {combo['i']}/{len(cseq)}: {tok:<3} "
                                  f"(opener {combo['opener']}, foe kind {kind}, our move "
                                  f"{my_mv_tick} phase {me.get('Phase')}, dist {d_c:.0f})"
                                  f"{'  (again x' + str(combo['again']) + ')' if repress else ''}")
            # ---- throws are not all alike (manual: T grabs standing, 2T
            # grabs crouching, only T can be escaped - press T the moment
            # they grab). Learn each throw's CommandCode / height once.
            if kind == MK_THROW and mv < 40000 and foe.get("CommandCode") < 10000:
                cmd_now, hml_now = foe.get("CommandCode"), foe.get("HighMidLowGround")
                if cmd_now and (mv not in throw_cmd or not throw_cmd[mv][0]):
                    throw_cmd[mv] = (cmd_now, hml_now)
                    ent = throws_seen.setdefault(str(fchar), {}).setdefault(
                        str(mv), {"cmd": {}, "hml": {}})
                    ent["cmd"][str(cmd_now)] = ent["cmd"].get(str(cmd_now), 0) + 1
                    ent["hml"][str(hml_now)] = ent["hml"].get(str(hml_now), 0) + 1
                if foe.get("CurrentMoveFrame") <= 3 and str(mv) in throws_seen.get(str(fchar), {}):
                    d_t, _ = distance()
                    if 20 < d_t < 400:          # how far out this throw starts from
                        ent = throws_seen[str(fchar)][str(mv)]
                        ent["reach"] = max(ent.get("reach", 0), round(d_t))
                if cmd_now or last_throw_start[0] != mv:
                    last_throw_start[:] = [mv, cmd_now or throw_cmd.get(mv, (0, 0))[0], hml_now]
            # ---- grabbed: tap T at once (and again on every combo-throw
            # part, which shows as a new kind-4 move while we stay type 5)
            if kind == 4 and my_type_now == MT_THROWN and not args.dry_run:
                if escape["mv"] != mv:
                    if escape["mv"] is not None and now - escape["t"] < 3.0 \
                            and me.get("CurrentHealth") >= escape["hp"]:
                        escape["ok"] += 1       # the previous part cost nothing
                        escape["by"].setdefault(escape["key"], [0, 0])[0] += 1
                    if zoning is not None:
                        inj.up(zoning); zoning = None
                    st_mv, st_cmd, st_hml = last_throw_start
                    if escape["seq"] is None or now - escape["seq"]["t"] > 3.0 \
                            or escape["seq"]["st"] != st_mv:
                        # first grab of this throw: pick how to break it. A
                        # later part (8174 -> 8178) keeps the same input
                        if escape["seq"] is not None:
                            finish_escape_seq()
                        opt = pick_escape(fchar, st_mv, st_cmd)
                        # were we inside an attack (type 1, an attack id) in
                        # the 8 frames before the grab? then it is hi-counter
                        recent = [h for h in history[-14:] if now - h[0] < 0.14]
                        older = [h for h in history[-14:] if now - h[0] >= 0.14]
                        if older:
                            recent.append(older[-1])   # the state in force when the window opened
                        hi = any(h[5] == 1 and 170 <= h[4] < 40000 for h in recent)
                        escape["seq"] = {"st": st_mv, "cmd": st_cmd, "opt": opt,
                                         "hp0": me.get("CurrentHealth"), "t": now, "hi": hi}
                    press_escape(escape["seq"]["opt"])
                    if (not last_answer["done"] and last_answer["mv"] == st_mv
                            and now - last_answer["t"] < 1.5):
                        record_answer(False)
                    key = f"{st_mv}/{mv} cmd {st_cmd} {throw_class(st_cmd, st_hml)}"
                    far_whiff[st_mv] = 0            # it reached us after all
                    if far_note[0] == st_mv:
                        far_note[0] = None
                    escape.update(mv=mv, t=now, hp=me.get("CurrentHealth"), key=key)
                    escape["tries"] += 1
                    escape["by"].setdefault(key, [0, 0])[1] += 1
                    print(f"  !    grabbed: {mv} (from {st_mv}, cmd {st_cmd}, "
                          f"h/m/l {st_hml}, {throw_class(st_cmd, st_hml)}) -> "
                          f"{escape['seq']['opt']} to break it")
                    facing_dirty[0] = True      # a throw usually swaps the sides
            elif escape["mv"] is not None and my_type_now != MT_THROWN \
                    and kind != 4 and now - escape["t"] > 0.15:
                if me.get("CurrentHealth") >= escape["hp"]:
                    escape["ok"] += 1
                    escape["by"].setdefault(escape["key"], [0, 0])[0] += 1
                escape["mv"] = None
                finish_escape_seq()
            if my_type_now == 17:
                if not was_intro[0] and not args.dry_run and pos_ok[0] \
                        and pos_ok[2] is False:
                    pos_ok[2] = True           # once per intro: rows may have moved
                    pos_ok[0] = False
                    pos_ok[1] = time.perf_counter() - 0.5
                was_intro[0] = True
                if (kind == 0 and mv == 0 and not args.dry_run and facing_dirty[0]
                        and now - last_probe[0] > 0.25 and my_mv_tick == 0):
                    # the guard tap proved inputs land during 17; a right
                    # tap here settles the facing before the first attack
                    # (the CPU's opener comes on the frame 17 ends)
                    if probe_facing() is not None:
                        probe_dirty_n[0] += 1
                if (kind in MK_STRIKES and not args.dry_run
                        and now - intro_tap[0] > 0.10):
                    # The CPU attacks while we still read 17. If 17 is a pose
                    # a button can cancel, Guard is the safe button to try.
                    intro_tap[0] = now
                    inj.down(["free"]); time.sleep(0.030); inj.up(["free"])
                    if intro_tap[1] == 0:
                        intro_tap[1] = 1
                        print("  intro: tapping Guard while they attack (17 cancel test)")
            elif was_intro[0] and not args.dry_run:
                was_intro[0] = False
                pos_ok[2] = False
                if kind == MK_THROW or my_type_now != MT_IDLE:
                    pass                # the opening run-throw: answered below;
                                        # or we are already being thrown
                elif args.poke != "none":
                    print("  round start: poking (the CPU opens with a run-throw)")
                    last_fire = poke("bell") or last_fire
                else:
                    print("  round start: backing off")
                    backdash()
                    last_fire = time.perf_counter()
                    last_action[:] = ["back", last_fire]
                if kind != MK_THROW:
                    time.sleep(period)
                    continue

            if salvage[0]:
                # a hold went in a moment ago. In 3-way mode a mirrored 4H is
                # 6H = plain guard (270), a mirrored 1H is 3H = crouch guard
                # (271). The tap ends before the strike lands, so KEEP H
                # down instead and eat a block, not a counter hit.
                if now > salvage[0]:
                    salvage[0] = 0.0
                elif guarding is None:
                    mm = me.get("CurrentMove")
                    if mm in GUARD_MOVES:
                        names = ["down"] if mm == 271 else []
                        inj.down(names + ["free"])
                        guards += 1
                        guarding = {"names": names, "mv": salvage[1],
                                    "t0": now, "until": now + 0.6,
                                    "crouch": mm == 271, "known": 1,
                                    "idx": guards}
                        if mm == 270:          # 6H: the horizontal was mirrored
                            facing_state[0] = not facing_state[0]
                            facing_fixes[0] += 1
                        salvage[0] = 0.0
                        print(f"  ={guards:<4} hold came out as guard ({mm}): "
                              f"holding H through the strike"
                              f"{', facing flipped' if mm == 270 else ''}")
            if not (incoming_strike or incoming_throw):
                armed = True
                my_mv_now = me.get("CurrentMove")
                my_free = (me.get("MoveType") in (MT_IDLE, MT_STRIKE)
                           and my_mv_now in (0, WALK_FWD, WALK_BACK))
                if (my_free and not prev_free[0] and kind == 0 and mv == 0
                        and not args.dry_run and my_mv_now == 0):
                    # just back on our feet: the sides may have swapped
                    probe_facing()
                prev_free[0] = my_free
                foe_idle = (kind == 0 and mv in (0, 1, 2, 3, 10, 31, 33, 39))
                d_now, _ = distance()
                d_reach = danger_reach() if not args.dry_run else None
                in_danger = (d_reach is not None and foe_idle
                             and d_now <= d_reach + 25)
                ct_mode = ("idle" if my_mv_now == 0 else
                           "walk" if (my_mv_now in BACK_IDS and zoning is not None) else None)
                if (args.close_throw_range > 0 and foe_idle and ct_mode is not None
                        and in_danger           # 29/47 vs the grappler, 0/14 vs everyone else
                        and d_now <= args.close_throw_range
                        and (my_free or ct_mode == "walk")     # a back DASH (4) is not "free"
                        and precrouch is None and not careful[0] and ct_allowed(ct_mode)
                        and close_throw["done"] and now - last_fire > 0.5
                        and mv in (0, 1, 2, 3)):
                    # also out of the back-off walk: 8137 caught us walking
                    # back inside its reach twice in one match, the standing
                    # T first never got the chance
                    if zoning is not None:
                        inj.up(zoning); zoning = None
                        time.sleep(0.02)
                    bt = best_throw(my_char)
                    if bt:
                        press_best_throw(bt)
                    else:
                        inj.down(["throw"]); time.sleep(args.press); inj.up(["throw"])
                    close_throw.update(t=now, hp0=foe.get("CurrentHealth"),
                                       myhp0=me.get("CurrentHealth"), done=False,
                                       mode=ct_mode)
                    last_fire = now
                    last_action[:] = ["throw", now]
                    print(f"  T    {'idle' if ct_mode == 'idle' else 'walking back'} at {d_now:.0f}: throwing first"
                          + (f"  (inside {danger_cache['mv']}'s reach)" if in_danger else ""))
                    time.sleep(period)
                    continue
                if precrouch is not None and not (in_danger and now < cornered[0]):
                    inj.up(precrouch); precrouch = None      # room again, or they moved
                if (in_danger and now < cornered[0] and precrouch is None
                        and my_mv_now == 0 and my_free):
                    # (my_free, not MoveType 2: our side reads type 1 while
                    # standing idle in some matches - 61 back-offs, 0 crouches)
                    # cornered inside the reach: a poke here is a hi-counter
                    # throw for them. Sit down - the standing throw whiffs on
                    # a croucher - until they move or we get room
                    if zoning is not None:
                        inj.up(zoning); zoning = None
                    precrouch = back_names() + ["down"]
                    inj.down(precrouch[:-1]); time.sleep(0.008); inj.down(precrouch[-1:])
                    danger_stats[0] += 1
                    last_action[:] = ["precrouch", now]
                    if danger_stats[0] <= 3 or danger_stats[0] % 10 == 0:
                        print(f"  v    danger: {danger_cache['mv']} ({danger_cache['su']} f, "
                              f"reach {d_reach:.0f}) and no room - crouching under it")
                    time.sleep(period)
                    continue
                if precrouch is not None:
                    time.sleep(period)
                    continue
                after_hold_2t = (combo_seqs.get("ground") and kind == 12 and d_now <= 130
                                 and now - last_hold_caught[0] < 3.0)
                if ((kind == 12 or (kind == 13 and 60 <= mv <= 110))
                        and not args.dry_run and not after_hold_2t
                        and my_free and my_mv_now == 0 and d_now <= 200
                        and now - wake_back[0] > 1.5):
                    # they are getting up; the next thing is a dash throw
                    # from ~180. Be at 220+ when it starts.
                    wake_back[0] = now
                    if zoning is None:
                        zoning = back_names()      # a walk: cancellable at once
                        inj.down(zoning)
                        zone_t0[:] = [now, d_now]
                    last_action[:] = ["back", now]
                    print(f"  <    wake-up: walking back (dist {d_now:.0f})")
                    time.sleep(period)
                    continue
                if (kind == 8 and args.poke != "none" and not args.dry_run
                        and combo["hp0"] is None
                        and my_free and my_mv_now == 0 and d_now <= args.poke_max
                        and now - last_fire > 0.20 and now - last_poke[0] > 0.30):
                    # they are in hit stun from our last hit: keep hitting,
                    # or they recover first and throw us out of our recovery
                    if zoning is not None:
                        inj.up(zoning); zoning = None
                    last_fire = poke("follow") or last_fire
                    armed = False
                    time.sleep(period)
                    continue
                dist_hist.append((now, d_now))
                if len(dist_hist) > 60:
                    del dist_hist[:30]
                old = [d for t, d in dist_hist if now - t >= 0.15]
                closing = bool(old) and old[-1] - d_now > 6      # they walk in
                # No pokes at a rising opponent: 0 for 5, and the kick's
                # recovery is exactly when the wake-up throw arrives.
                # Inside 150 an idle CPU is one 7-frame dash throw (8340)
                # away from 35 damage, and nothing reactive beats 7 frames.
                # A strike beats a throw, so hit first whenever it stands or
                # walks there; farther out only when it is closing in.
                if (args.poke != "none" and not args.dry_run and my_free
                        and now - last_poke[0] > poke_gap_now()
                        and now - round_end[0] > 4.0    # KO camera + intro: 23 dead pokes read
                                                        # as misses and tripped the throttle
                        and now - last_fire > 0.3 and foe_idle and not in_danger
                        and me.get("CurrentMove") == 0
                        and args.poke_min <= d_now <= min(args.poke_max, args.jab_reach)):
                    if zoning is not None:
                        inj.up(zoning)
                        zoning = None
                    last_fire = poke("wall" if now < cornered[0] else
                                     "far" if d_now > args.jab_reach else "range") or last_fire
                    armed = False
                    time.sleep(period)
                    continue
                if args.zone and not args.dry_run and my_free and foe_idle \
                        and now >= cornered[0]:
                    if zoning is None and d_now < max(args.zone, (d_reach or 0) + 25):
                        zoning = back_names()
                        inj.down(zoning)
                        zone_t0[:] = [now, d_now]
                        if in_danger:
                            danger_stats[1] += 1
                            if danger_stats[1] <= 3:
                                print(f"  <    danger: {danger_cache['mv']} reaches "
                                      f"{d_reach:.0f} - backing off instead of poking")
                    elif zoning is not None:
                        if (now - zone_t0[0] > 0.35 and d_now <= zone_t0[1] + 5
                                and (my_mv_now in FWD_IDS + BACK_IDS
                                     or me.get("MoveType") != 17)):
                            # walking back gained nothing: a wall. Stop
                            # retreating for a while and fight instead.
                            # (not during the round intro, 17: the key does
                            # not move us there, and "cornered" at the bell
                            # put us in a crouch under the opening mid punch)
                            inj.up(zoning)
                            zoning = None
                            cornered[0] = now + 3.0
                            print("  cornered: no room to back off, poking instead")
                            time.sleep(period)
                            continue
                        # the walk id is the truth about which way we went
                        if my_mv_now in FWD_IDS and now - zone_flip[0] > 0.12:
                            # forward: mirrored. The walk id lags the key by
                            # 2-3 frames, so without this debounce the flip
                            # re-fired every tick (41 silent flips a match)
                            inj.up(zoning)
                            facing_state[0] = not facing_state[0]
                            facing_fixes[0] += 1
                            zone_flip[:] = [now, zone_flip[1] + 1]
                            zoning = back_names()
                            inj.down(zoning)
                        elif d_now >= args.zone_out:
                            inj.up(zoning)
                            zoning = None
                    time.sleep(period)
                    continue
                if zoning is not None:
                    inj.up(zoning)
                    zoning = None
                if (not args.dry_run and kind == 0
                        and (mv == 0 or mv in FWD_IDS + BACK_IDS)
                        and me.get("MoveType") == MT_IDLE
                        and me.get("CurrentMove") == 0
                        and (facing_dirty[0] and now - last_probe[0] > 0.25
                             and now - last_fire > 0.3
                             or now - last_probe[0] > (0.7 if input_lag[0] > 80 else 1.5)
                             and now - last_throw_seen[0] > 1.0
                             and now - last_fire > 0.6)):
                    # a walking CPU is as safe to probe against as an idle
                    # one (the tap is one frame), and a dirty facing gets
                    # probed at the first quiet moment, not 1.5 s later
                    was_dirty = facing_dirty[0]
                    if probe_facing() is not None and was_dirty:
                        probe_dirty_n[0] += 1
                time.sleep(period)
                continue
            if zoning is not None:                 # something is coming
                inj.up(zoning)
                zoning = None
                if kind in MK_STRIKES:
                    # a hold from a walk is 1/3: back held for seconds is
                    # "guard", and down+H on top of it is a crouch guard
                    # (271), not 1H. Let the game see the release first.
                    t_w = time.perf_counter()
                    while time.perf_counter() - t_w < 0.050:
                        me.refresh()
                        if me.get("CurrentMove") not in FWD_IDS + BACK_IDS:
                            break
                        time.sleep(0.002)
                    time.sleep(0.008)
                else:
                    time.sleep(0.012)   # else back is still down and P comes out as 4P
            if not armed or now - last_fire < args.cooldown:
                if not (kind == MK_THROW and ph == PH_STARTUP
                        and throw_epoch[0] != answered_epoch[0]):
                    time.sleep(period)
                    continue
                # a new throw right behind the last one: answer it anyway

            dist, facing_right = distance()

            # We cannot act while our own command is still playing out - a
            # hold's recovery runs to frame ~33, a guard as long as we hold
            # Free. Six inputs in one session produced "no reaction" for
            # exactly that reason. Hit stun is fine: DOA lets you hold there.
            # Act only when we are free to: MoveType 2 is idle/movement.
            # 1 = our own command still playing (hold recovery runs to frame
            # ~33, a guard as long as Free is down), 3 = hit stun, and the
            # other values (thrown, downed, staggered - id 24153 read as
            # something other than 3) are all states where the input dies.
            # MoveType 2 alone is not the test: our own side reads 1 while
            # standing idle in some matches (0 holds fired in a whole session
            # when this demanded 2). So: busy = a command of ours is playing,
            # stunned = hit stun, thrown, or a hit-reaction animation id.
            my_type = me.get("MoveType")
            my_move = me.get("CurrentMove")
            busy = ((my_type == MT_STRIKE
                     and my_move not in (0, WALK_FWD, WALK_BACK) + GUARD_MOVES)
                    or my_move in CROUCH_MOVES)     # 3rd time: inputs from
                                                    # 125-135 come out as 80-91
            if precrouch is not None and (incoming_strike or incoming_throw):
                # sitting under a fast throw's reach: a strike needs us up
                # (holds are skipped from 10/13); a standing throw wants
                # exactly this crouch, so keep it and let the duck answer
                # run. A LOW throw (char 2's 8161, the other half of its
                # mixup) grabs crouchers: stand up at once
                t_cmd0, t_hml0 = throw_cmd.get(mv, (None, None))
                if incoming_strike or (incoming_throw and throw_class(t_cmd0, t_hml0) == "low"):
                    inj.up(precrouch); precrouch = None
                elif my_move in (10, 13):
                    busy = False
            in_reaction = (my_type == MT_HIT
                           or HIT_REACTION[0] <= my_move < HIT_REACTION[1])
            # DOA6 lets a stunned character hold: that is the whole stun
            # game, and it is the ONLY thing we can do there (no guard, no
            # step). 460 of 806 damage in one match came while we stood in
            # a stun doing nothing. Type 3 with a 24xxx-26xxx reaction id
            # only - 5/6/7 (thrown, hold-hit, downed) and the 32xxx air
            # states are not holdable - and never in a stun where three
            # tries all went unregistered.
            stun_hold_ok = (args.hold_in_stun and in_reaction and my_type == MT_HIT
                            and HIT_REACTION[0] <= my_move < HIT_REACTION[1]
                            and not stun_dead(my_move)
                            and me.get("CurrentMoveFrame") >= args.stun_hold_frame)
            # 17 = round intro: inputs DO work there (the guard tap came
            # out as 270), and the CPU starts 8144 while we still read 17.
            # Let the throw answer through; strikes in 17 still wait.
            stunned = ((my_type in (MT_HIT, MT_THROWN, MT_HOLD_HIT) or in_reaction
                        or (my_type == 17 and kind != MK_THROW))
                       and not stun_hold_ok)   # 6: our own hold throw is playing
                                               # (two holds fired from 8195, 0/2)
            if busy or stunned:
                if stunned:
                    skipped_stun += 1
                if skipped_note[0] != mv:
                    skipped_note[0] = mv
                    what = "throw" if kind == MK_THROW else STRIKE_TYPE.get(
                        foe.get("StrikeType"), "strike")
                    print(f"  (skip) {what:<11} move={mv:<6} frame={fr:<3} - we are "
                          f"{'stunned' if stunned else 'busy'} (our move {my_move}, "
                          f"type {my_type})")
                time.sleep(period)
                continue
            skipped_note[0] = None

            if incoming_throw and not incoming_strike:
                if args.throw_answer == "none" or (
                        dist >= args.throw_punish_range
                        and now - pos_suspect[0] > 5.0):
                    # out of range: nothing to answer. Only trust that while
                    # the distance itself is trustworthy - with a bad position
                    # row every throw reads as out of range and goes
                    # unanswered, which is how a 22-0 run ended
                    time.sleep(period)
                    continue
                if far_whiff.get(mv, 0) >= 3 and dist >= 140:
                    # char 28's 8183 was started from 175-185 sixty times in a
                    # row; every duck answered air and the round ran out on
                    # the clock. Out there it cannot reach us: let it whiff
                    # and keep the poke / zoning logic running instead
                    if far_note[0] != mv:
                        far_note[0] = mv
                        print(f"  ~    throw {mv} whiffed {far_whiff[mv]}x in a row from "
                              f"{dist:.0f}: out of its reach - not answering it from 140+")
                    time.sleep(period)
                    continue
                mv0 = me.get("CurrentMove")
                answered_epoch[0] = throw_epoch[0]
                t_known = startup.get(fchar, mv)
                t_left = (t_known - fr) if t_known is not None else None
                answer = args.throw_answer
                t_cmd, t_hml = throw_cmd.get(mv, (None, None))
                t_cls = throw_class(t_cmd, t_hml)
                if answer == "crouch" and t_cls in ("low", "T"):
                    # 2T grabs crouchers only and we stand; the plain T we
                    # escape by pressing T as it grabs (4/4). Stay put, and
                    # punish the whiff. Unless this one's break keeps
                    # failing (8181 -> 8252, cmd 111, 0/3): then duck/back/side.
                    # Scored and saved like the other answers: char 4's 8202
                    # starts as the T throw (cmd 363) and turns into 8245
                    # (cmd 386, unbreakable) - the in-session counter paid
                    # for that lesson again every run (42-1, 54-1, 57-1)
                    answer = pick_answer(fchar, mv, "wait",
                                         options=("wait", "duck", "back", "side", "lowkick"))
                    if answer == "wait" and wait_failed.get(mv, 0) >= 2:
                        answer = pick_answer(fchar, mv, "duck")
                elif answer == "crouch":
                    jab_need = max(args.throw_jab_frames,
                                   CHAR_P_STARTUP.get(my_char, 12)
                                   + int(round(input_lag[0] / (1000 / 60))) + 1)
                    if (t_left is not None and t_left >= jab_need
                            and dist <= args.jab_reach
                            and me.get("CurrentMove") == 0
                            and mv not in BACK_THROWS):
                        answer = "jab"      # close, slow, and we are standing
                                            # still (a walk turns P into 4P/6P
                                            # and loses the race by a frame)
                    elif t_hml == 1 or t_hml is None:
                        # a standing throw grabs nothing on a croucher. Down
                        # ALONE is a sidestep in DOA6 (ids 31/32, grabbed 42
                        # times); down+back is the crouch (10 -> 13). Unless
                        # this throw has shown it grabs crouchers: then the
                        # table says back or side.
                        # a fast throw (<= 8 f to the grab) beats the crouch
                        # walk every time (8137 1/14, 8004 0/6) but not the
                        # low kick's instant crouching status (8137 6/6,
                        # 8003 4/4): start those on the low kick
                        fast = t_known is not None and t_known <= 8
                        answer = pick_answer(fchar, mv, "lowkick" if fast else "duck",
                                             explore=True)
                    else:
                        answer = pick_answer(fchar, mv, "back")
                if answer in ("back", "side", "duck", "lowkick", "hopkick", "wait"):
                    if not close_throw["done"]:
                        close_throw["done"] = True  # our T was cut short by this
                                                    # answer: neither a hit nor a miss
                    if not last_answer["done"]:
                        record_answer(True)     # the previous one was not punished
                    last_answer.update(mv=mv, ans=answer, t=time.perf_counter(), done=False)
                    if t_left is not None and t_left <= 3:
                        # noticed too late (we were busy poking): the grab
                        # that follows says nothing about the answer. 8284
                        # booked three of those against a duck that is
                        # 107/124 when it gets to start in time
                        last_answer["done"] = True
                if answer in ("back", "side", "duck", "wait", "lowkick", "hopkick"):
                    if answer in ("duck", "wait"):
                        key = (back_names() + ["down"]) if answer == "duck" else []
                        # interruptible: the CPU chains throws (8148 whiff ->
                        # 8144 grab); a blocking crouch hid the second one
                        # until frame 14. Stay down while THIS throw lasts.
                        # Stay down until THIS throw is in recovery or
                        # gone: a fixed 0.25 s stood us up on the exact
                        # frame 8144 (16 f startup) became active. If no
                        # crouch id shows after 4 frames, press again.
                        if key:
                            # horizontal first, vertical 8 ms later: sent in
                            # one batch the game took the horizontal and lost
                            # the down 3 times (ours: 0>2>0>17, grabbed)
                            inj.down(key[:-1]); time.sleep(0.008); inj.down(key[-1:])
                        t_press = time.perf_counter()
                        t_end = t_press + 0.60
                        down_again = False
                        this_mv = mv
                        crouched = repressed = False
                        whiffed = False
                        ours = [me.get("CurrentMove")]
                        flipped_d = False
                        while time.perf_counter() < t_end:
                            foe.refresh(); me.refresh()
                            mm = me.get("CurrentMove")
                            if mm != ours[-1]:
                                ours.append(mm)
                            if mm in DUCK_IDS:
                                crouched = True
                            elif (mm in BACK_IDS and key and not crouched and not down_again
                                    and time.perf_counter() - t_press > 0.050):
                                inj.up(key[-1:]); time.sleep(0.005); inj.down(key[-1:])
                                down_again = True         # walking, not crouching: down again
                            elif mm in FWD_IDS + (16, 17, 18) and key and not flipped_d:
                                # that was down+forward: the facing guess is
                                # stale (usual after a throw). Other side.
                                # 16/17/18 are the forward-step states (the
                                # calibration read move 18 / cmd 17 while 6
                                # was held): "ours: 2>0>17" grabbed us for
                                # weeks without the flip firing
                                inj.up(key)
                                facing_state[0] = not facing_state[0]
                                facing_fixes[0] += 1
                                key = back_names() + ["down"]
                                inj.down(key)
                                flipped_d = True
                            fk, fm = foe.get("MoveKind"), foe.get("CurrentMove")
                            if fk == MK_THROW and fm == this_mv \
                                    and foe.get("Phase") == PH_RECOVERY:
                                whiffed = True
                                break               # it grabbed air: punish
                            if fm != this_mv and fk in (MK_THROW, 0, 3, 2, 4):
                                break               # it ended or changed: react
                            time.sleep(0.003)
                        if key:
                            inj.up(key)
                        # (the 4-frame re-press is gone: it restarted the
                        # crouch each time; 32 ducks, none came out)
                        if whiffed and answer == "duck":
                            record_answer(True)
                        if whiffed:
                            # "out of reach" only if it is STILL far when it
                            # grabs air: char 3's 8165 (23 f) dashes in from
                            # 150 and whiffed on our crouch three times - the
                            # old test then stopped answering it, and it grabbed
                            d_w, _ = distance()
                            far_whiff[mv] = (far_whiff.get(mv, 0) + 1
                                             if dist >= 140 and d_w >= 140 else 0)
                        note = ("" if crouched or not key else "  (no crouch id)") \
                               + ("  (facing flipped)" if flipped_d else "") \
                               + ("  (down re-pressed)" if down_again else "") \
                               + "  ours: " + ">".join(str(m) for m in ours[:8])
                        if whiffed and args.punish_after_guard != "none":
                            d2, _ = distance()
                            # no throw here: we are still crouching, and T
                            # from a crouch is the LOW throw (8145), which
                            # grabs crouchers only - it whiffed 18/18 on the
                            # standing CPU. The punch landed 6/6
                            # and not from 159+: the long punch there (8020)
                            # whiffed twice and its recovery ate the next 8148
                            # a short-armed character whiffs the standing P
                            # past ~100 (Kula 176: 2 of 13 landed at 94-137)
                            reach = min(args.punish_punch_range, CHAR_PUNCH_REACH.get(my_char, 155))
                            btn = "punch" if d2 <= reach else None
                            if btn:
                                pre = {"mv": me.get("CurrentMove"),
                                       "mt": me.get("MoveType"), "ph": 0,
                                       "fmv": fm, "fph": foe.get("Phase")}
                                jab(inj, btn, press=args.press)
                                punished += 1
                                by_kind.setdefault("punish", [0, 0])[1] += 1
                                fire("punish", "~ duck punish", me.get("CurrentMove"),
                                     None, pre)
                                note += f"  (whiffed) -> {btn} @{d2:.0f}"
                    elif answer in ("lowkick", "hopkick"):
                        note = ""
                        if zoning is not None:
                            inj.up(zoning); zoning = None
                        combo_press(parse_combo("2K" if answer == "lowkick" else "8K")[0])
                    else:
                        note = ""
                        (backdash if answer == "back" else sidestep)()
                    last_fire = time.perf_counter()
                    armed = False
                    ducked += 1
                    last_action[:] = [answer, last_fire]
                    print(f"  ~    throw {mv:<6} -> {answer:<7} dist={dist:.0f} "
                          f"frame={fr} left={t_left if t_left is not None else '?'}"
                          f"  [cmd {t_cmd} h/m/l {t_hml} {t_cls}]{note}")
                    time.sleep(period)
                    continue
                if answer == "jab":
                    jab(inj, "punch", press=args.press)
                    last_fire = time.perf_counter()
                    armed = False
                    by_kind.setdefault("jab", [0, 0])[1] += 1
                    fire("jab", "~ jab", mv0, None)
                    last_action[:] = ["jab", last_fire]
                    print(f"  ~    throw {mv:<6} -> punch   dist={dist:.0f} "
                          f"frame={fr} left={t_left if t_left is not None else '?'}")
                    time.sleep(period)
                    continue
                # duck: a standing throw grabs nothing on a crouching target
                inj.down(["down"])
                time.sleep(args.crouch_time)
                inj.up(["down"])
                last_fire = time.perf_counter()
                armed = False
                ducked += 1
                last_action[:] = ["duck", last_fire]
                time.sleep(0.04)
                me.refresh(); foe.refresh()
                d2, _ = distance()
                whiffed = (foe.get("MoveKind") == MK_THROW
                           and foe.get("Phase") == PH_RECOVERY)
                note = ""
                if whiffed and args.punish_after_guard != "none" \
                        and me.get("MoveType") == MT_IDLE:
                    btn = ("throw" if d2 <= args.punish_throw_range else
                           "punch" if d2 <= args.punish_punch_range else None)
                    bt = best_throw(my_char) if btn == "throw" else None
                    if btn:
                        pre = {"mv": me.get("CurrentMove"), "mt": me.get("MoveType"),
                               "ph": 0, "fmv": foe.get("CurrentMove"),
                               "fph": foe.get("Phase")}
                        if bt:
                            press_best_throw(bt)
                        else:
                            jab(inj, btn, press=args.press)
                        punished += 1
                        by_kind.setdefault("punish", [0, 0])[1] += 1
                        fire("punish", f"~ duck punish", me.get("CurrentMove"),
                             None, pre)
                        last_action[:] = ["punish", time.perf_counter()]
                        note = f"  -> {btn} @{d2:.0f}"
                if whiffed:
                    d_w, _ = distance()
                    far_whiff[mv] = (far_whiff.get(mv, 0) + 1
                                     if dist >= 140 and d_w >= 140 else 0)
                print(f"  ~    throw {mv:<6} -> step    dist={dist:.0f} "
                      f"frame={fr} left={t_left if t_left is not None else '?'}"
                      f"{'  (their throw whiffed)' if whiffed else ''}{note}")
                time.sleep(period)
                continue

            if (mv in oh_seen.get(str(fchar), ()) and ph == PH_STARTUP
                    and not args.dry_run and dist <= 260):   # 8021 (a running OH) from 231
                # an offensive hold beats holds. What else it beats differs
                # per move: 8009 grabbed a sidestep, then HIT a croucher (198
                # dmg), so the answer is learned per move like the throws:
                # crouch, block, back off, or step, scored by what hurt us.
                ans = pick_answer(fchar, mv, "duck", options=("duck", "guard", "back", "side"))
                if not last_answer["done"]:
                    record_answer(True)
                last_answer.update(mv=mv, ans=ans, t=time.perf_counter(), done=False)
                ours = [me.get("CurrentMove")]
                if ans == "duck":
                    key = back_names() + ["down"]
                    inj.down(key[:-1]); time.sleep(0.008); inj.down(key[-1:])
                    t_press = time.perf_counter()
                    while time.perf_counter() - t_press < 0.6:
                        foe.refresh(); me.refresh()
                        mm = me.get("CurrentMove")
                        if mm != ours[-1]:
                            ours.append(mm)
                        if foe.get("CurrentMove") != mv or foe.get("Phase") == PH_RECOVERY:
                            break
                        time.sleep(0.003)
                    inj.up(key)
                elif ans == "guard":
                    inj.down(["free"])
                    guards += 1
                    guarding = {"names": [], "mv": mv, "t0": time.perf_counter(),
                                "until": time.perf_counter() + 0.7, "crouch": False,
                                "known": 1, "idx": guards}
                elif ans == "back":
                    backdash()
                else:
                    sidestep()
                last_fire = time.perf_counter()
                armed = False
                oh_stats[0] += 1
                last_action[:] = [ans, last_fire]
                print(f"  ~    OH {mv:<6} -> {ans:<7} dist={dist:.0f} frame={fr}"
                      + (("  ours: " + ">".join(str(m) for m in ours[:8])) if ans == "duck" else ""))
                time.sleep(period)
                continue
            st = foe.get("StrikeType")
            if st not in STRIKE_TO_HOLD:
                time.sleep(period)
                continue
            known = startup.get(fchar, mv)
            excepted = mv in set(exceptions.get(str(fchar), [])) | set(exceptions.get("*", []))
            block = st in guard_types or excepted \
                or mv in nohold_seen.get(str(fchar), ())

            if known is None:
                if fr <= 2:                     # once per sighting
                    unknown_seen[mv] = startup.note_unknown(fchar, mv)
                else:
                    unknown_seen.setdefault(mv, startup.seen.get((fchar, mv), 0))
            is_runup = (known is None and args.approach_attack != "none"
                        and not careful[0] and unknown_seen.get(mv, 0) >= 3)
            if is_runup and dist > args.approach_range and fr >= 18                     and mv in LUNGE_PRECURSORS and me.get("CurrentMove") == 0:
                # 188 -> 8149 -> 8183: 23 frames of run-up, then a lunge that
                # covers 250 units in 15 frames and grabs crouchers. Nothing
                # reactive works; a kick thrown at frame 18 is live at ~34,
                # right as the lunge arrives. Strike beats throw.
                mv0 = me.get("CurrentMove")
                pre = {"mv": mv0, "mt": me.get("MoveType"), "ph": 0,
                       "fmv": mv, "fph": ph}
                jab(inj, "kick", press=args.press)
                last_fire = time.perf_counter()
                armed = False
                approach_hits[0] += 1
                by_kind.setdefault("approach", [0, 0])[1] += 1
                fire("approach", f"> lunge-bait {mv}", mv0, None, pre)
                last_action[:] = ["approach", last_fire]
                print(f"  >    run-up {mv:<6} -> kick   dist={dist:.0f} frame={fr}  "
                      f"(lunge bait: the kick meets 8149 as it arrives)")
                time.sleep(period)
                continue
            if is_runup and (fr < 4 or dist > args.approach_range):
                time.sleep(period)              # wait: too early, or too far
                continue                        # for a punch to connect
            if is_runup:
                # Never went active in three sightings: not a strike, a
                # run-up into a throw. In reach, a strike beats the throw;
                # out of reach a whiffed punch just hands them the grab, so
                # back off and let the runner grab air.
                mv0 = me.get("CurrentMove")
                if dist > args.approach_range:
                    time.sleep(period)      # not yet: a backdash cannot outrun it,
                    continue                # a punch from here whiffs; let it come
                pre = {"mv": mv0, "mt": me.get("MoveType"), "ph": 0,
                       "fmv": mv, "fph": ph}
                jab(inj, args.approach_attack, press=args.press)
                what = args.approach_attack
                by_kind.setdefault("approach", [0, 0])[1] += 1
                fire("approach", f"> approach {mv}", mv0, None, pre)
                last_fire = time.perf_counter()
                armed = False
                approach_hits[0] += 1
                last_action[:] = ["approach", last_fire]
                print(f"  >    run-up {mv:<6} -> {what:<6} "
                      f"dist={dist:.0f} frame={fr}  (seen {unknown_seen[mv]}x "
                      f"across sessions, never active)")
                time.sleep(period)
                continue
            if known is None and not block:
                if args.unknown == "skip":
                    skipped_unknown += 1
                    armed = False
                    time.sleep(period)
                    continue
                if stun_hold_ok:
                    # in a stun a guard does nothing: hold by the strike
                    # type once the startup is a few frames old, as
                    # --unknown hold would
                    if fr < args.unknown_frame:
                        time.sleep(period)
                        continue
                elif args.unknown == "guard":
                    block = True
                elif fr < args.unknown_frame:
                    time.sleep(period)
                    continue
            remaining = (known - fr) if known is not None else None

            # A hold needs (input lag + ~4 frames of catch window) before the
            # strike goes active; a guard only needs the lag. When the frames
            # left are not enough for the hold, BLOCK instead of doing nothing
            # - under a 90 ms lag that is most 11-13 frame moves.
            late_guard = False
            if remaining is not None and not block:
                need = (max(args.min_remaining, round(input_lag[0] / (1000 / 60)) + 3)
                        if input_lag[0] > 80 else args.min_remaining)
                if remaining < need:
                    block = late_guard = True
                    skipped_late += 1
            if block and stun_hold_ok:
                # a guard does nothing in a stun; the hold is the only input
                # the game takes there, so throw it even when it looks late
                block = False
                if late_guard:
                    skipped_late -= 1
                    late_guard = False

            if block:
                crouch = foe.get("HighMidLowGround") == 3
                names = ["down"] if crouch else []
                inj.down(names + ["free"])
                up_at = time.perf_counter()
                guards += 1
                guarding = {"names": names, "mv": mv, "t0": up_at,
                            "until": up_at + 0.8, "crouch": crouch,
                            "known": known, "idx": guards}
                last_fire = up_at
                armed = False
                last_action[:] = ["guard", up_at]
                print(f"  ={guards:<4} {STRIKE_TYPE[st]:<11} -> "
                      f"{'crouch' if crouch else 'stand':<6} guard  move={mv:<6} "
                      f"frame={fr:<3} remaining={remaining if remaining is not None else '?':<3} "
                      f"dist={dist:.0f} lat={(up_at - now) * 1000:.1f}ms"
                      f"{'  (startup unknown - learning)' if known is None else ''}"
                      f"{'  (too late to hold: block)' if late_guard else ''}")
                time.sleep(period)
                continue

            if dist >= args.distance:
                time.sleep(period)          # a hold from out here only whiffs
                continue

            if remaining is not None:
                if remaining > args.window:
                    time.sleep(period)
                    continue
                need = max(args.min_remaining, round(input_lag[0] / (1000 / 60)) + 3)                     if input_lag[0] > 80 else args.min_remaining
                if remaining < need and not stun_hold_ok:
                    skipped_late += 1
                    armed = False
                    time.sleep(period)
                    continue

            kind = STRIKE_TO_HOLD[st]
            # 4S with half a Break Gauge catches high, mid AND low: no
            # StrikeType to trust, no 3-way/4-way question, no mid-startup
            # id swap to be fooled by. Spend it whenever we have it.
            gauge_est = gauge_now()
            gauge_raw = me.get("BreakGauge")
            use_break = args.break_hold and gauge_est >= break_need[0]
            if use_break:
                kind = "midp"                    # 4 + S: plain back
            mv0 = me.get("CurrentMove")
            held = keys_physically_down()
            if held:
                inj.up(list(held))             # a direction left down turns the
                time.sleep(0.004)              # hold into a walk: clear it first
            pre = {"mv": mv0, "mt": me.get("MoveType"), "ph": me.get("Phase"),
                   "fmv": mv, "fph": ph, "fr": me.get("CurrentMoveFrame")}
            in_stun_now = pre["mt"] == MT_HIT
            _user32.GetAsyncKeyState(_VK["j"])      # clear the "pressed" bit
            # the walk id that tells us our facing takes ~42 ms to appear;
            # a 40 ms wait missed it on every wrong-facing hold. Wait
            # longer when the strike still gives us the frames.
            # wait for the walk id as long as the game actually takes to show
            # it (42 ms normally; ~90 ms in one match, when every probe timed
            # out and 31 of 32 holds came out mirrored), plus a frame
            lag = input_lag[0] / 1000
            o_wait = min(0.15, lag + 0.025) if (remaining is None or remaining >= 9)                 else min(0.10, lag + 0.005)
            if input_lag[0] > 80 or in_stun_now:
                o_wait = 0.0        # waiting for the walk id would cost 5+ frames:
                                    # trust the (now 0.7 s) probe and press at once.
                                    # In stun we cannot walk, so there is no
                                    # walk id to wait for either
            pressed_at, flipped = oracle_hold(kind, "special" if use_break else "free",
                                              wait=o_wait)
            if use_break:
                breaks[0] += 1
                gauge_spent[0] += args.gauge_max // 2
                kind = "break"
            if kind in ("midp", "midk", "low", "break"):
                salvage[:] = [time.perf_counter() + 0.10, mv]
            facing_right = facing_state[0]
            last_fire = time.perf_counter()
            armed = False
            count += 1
            by_kind.setdefault(kind, [0, 0])[1] += 1
            if remaining is not None:
                by_rem.setdefault(remaining, [0, 0])[1] += 1
            fire(kind, f"#{count} {kind} hold", mv0, remaining, pre)
            pending[-1]["gauge"] = (gauge_raw, gauge_est)
            pending[-1]["o_wait"] = o_wait
            pending[-1]["flipped"] = flipped
            if oracle_seen[0] is not None:
                # the probe saw the walk id: that IS the lag. (Measuring it
                # from the trace instead read "wait + 2 ms" and talked the
                # bot into high-lag mode with no lag at all.)
                pending[-1]["lag_known"] = True
                input_lag_hist.append(oracle_seen[0])
                if len(input_lag_hist) > 15:
                    del input_lag_hist[0]
                if len(input_lag_hist) >= 3:
                    med = sorted(input_lag_hist)[len(input_lag_hist) // 2]
                    if len(input_lag_hist) >= 5 and (not lag_reported[0]
                                                     or abs(med - input_lag[0]) > 10):
                        lag_reported[0] = True
                        print(f"  input lag: ~{med:.0f} ms from key to game "
                              f"({med / (1000 / 60):.1f} frames)"
                              + ("  - HIGH: holds go direction+H together" if med > 80 else ""))
                    input_lag[0] = med
            pending[-1]["dx"] = holds[kind if kind != "break" else "midp"][0]
            last_action[:] = ["hold", last_fire]
            print(f"  #{count:<4} {STRIKE_TYPE[st]:<11} -> {kind:<5} "
                  f"{'BREAK' if kind == 'break' else 'hold '} "
                  f"move={mv:<6} frame={fr:<3} "
                  f"remaining={remaining if remaining is not None else '?':<3} "
                  f"dist={dist:.0f} me={'R' if facing_right else 'L'} "
                  f"lat={(pressed_at - now) * 1000:.1f}ms"
                  + (f"  [!] keys already held: {'+'.join(held)}" if held else "")
                  + ("  (facing flipped by the probe)" if flipped else "")
                  + (f"  gauge est {gauge_est} (raw {gauge_raw}, need {break_need[0]})"
                     if kind == "break" else ""))
            if held:
                keys_held_count[0] += 1
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        if precrouch is not None:
            inj.up(precrouch)
        try:
            with open(THROWS_FILE, "w", encoding="utf-8") as fh:
                json.dump(throws_seen, fh, indent=1, sort_keys=True)
        except OSError:
            pass
        if guarding is not None:
            inj.up(guarding["names"] + ["free"])
        if zoning is not None:
            inj.up(zoning)
        inj.close()
        disable_high_res_timer()
        if pending:
            time.sleep(0.3)
            foe.refresh(); me.refresh()
            settle(force=True)
        startup.save()
        proc.close()
        print(f"\nstopped after {count} holds, {guards} guards   "
              f"rounds won {rounds[0]} / lost {rounds[1]}")
        if learned_now:
            print(f"  learned {learned_now} new startup value(s) -> startup.json")
        if skipped_unknown:
            print(f"  {skipped_unknown} skipped: startup unknown")
        never = {m: n for m, n in unknown_seen.items()
                 if startup.get(foe.get("CurrentCharacter"), m) is None}
        if never:
            print("  guarded but still no startup learned (move id x times): "
                  + "  ".join(f"{m}x{n}" for m, n in sorted(never.items())))
        if skipped_late:
            print(f"  {skipped_late} declined as too late")
        if skipped_stun:
            print(f"  {skipped_stun} ticks skipped while we were stunned / "
                  f"thrown / down")
        if count:
            print(f"  landed {landed}/{count} ({100 * landed / count:.0f}%)")
        for k, (ok, n) in sorted(by_kind.items()):
            print(f"    {k:<5} {ok:>3}/{n:<3} {100 * ok / n:>3.0f}%")
        if by_rem:
            print("  by frames remaining at input:")
            for r in sorted(by_rem, reverse=True):
                ok, n = by_rem[r]
                print(f"    remaining={r:<3} {ok:>3}/{n:<3}  "
                      f"{'#' * ok}{'.' * (n - ok)}")
        if no_reaction:
            print(f"  {no_reaction} inputs produced no reaction at all")
        if ducked or punished or approach_hits[0] or pokes[0]:
            if hold_dmg:
                print("  hold damage (foe hp lost while our hold played out): "
                      + ", ".join(f"{k} {t / n:.0f} avg x{n}" for k, (t, n) in sorted(hold_dmg.items())))
            bank = combo_bank.get(str(my_char), {})
            if bank:
                print(f"  strings tried for char {my_char} (mean NET dmg after the opener, tries):")
                for key, stats in sorted(bank.items()):
                    row = sorted(stats.items(), key=lambda kv: -(kv[1][1] / max(1, kv[1][0])))
                    print(f"    after {key:<8}: " + "  ".join(
                        f"{r} {d / max(1, n):.0f}x{n}" for r, (n, d) in row))
            if combo_stats["started"]:
                n = combo_stats["started"]
                print(f"  combos: {n} openers connected, {combo_stats['hits']} string "
                      f"inputs, {combo_stats['dmg']} dmg after the opener "
                      f"({combo_stats['dmg'] / n:.0f} per combo)  [--combo {args.combo}]")
                for ln, (c, dmg) in sorted(combo_stats["by_len"].items()):
                    print(f"    {ln} extra input(s): {c}x, {dmg / max(c, 1):.0f} dmg avg")
            if escape["tries"]:
                print(f"  throw escapes: break input pressed on {escape['tries']} grabs, "
                      f"{escape['ok']} parts cost no damage")
                for k, (ok, n) in sorted(escape["by"].items()):
                    print(f"    {k:<32} {ok}/{n}")
            if danger_stats[0] or danger_stats[1]:
                ct = ct_stats.get(f"{my_char}:{fchar}")
                ctw = ct_stats.get(f"walk:{my_char}:{fchar}")
                if ct or ctw:
                    print(f"  T first on an idle opponent inside {args.close_throw_range:.0f}: "
                          + (f"standing {ct[0]}/{ct[1]}" if ct else "standing 0/0")
                          + (f", out of the walk {ctw[0]}/{ctw[1]}" if ctw else "")
                          + f" grabbed them (char {fchar})")
                print(f"  fast-throw danger zone: backed off {danger_stats[1]}x, "
                      f"crouched under it {danger_stats[0]}x")
            esc = {k: v for k, v in throw_esc.items() if k.startswith("cmd")}
            if esc:
                print("  throw breaks learned by CommandCode (input: broken/tried):")
                def _cmd_num(k):
                    try:
                        return int(k[3:])
                    except ValueError:
                        return -1          # "cmdNone": a grab whose start code was never read
                for key, opts in sorted(esc.items(), key=lambda kv: _cmd_num(kv[0])):
                    print(f"    {key:<8} " + "  ".join(f"{o} {v[0]}/{v[1]}" for o, v in opts.items()))
            if oh_stats[0] or oh_stats[1]:
                print(f"  offensive holds: {oh_stats[1]} learned this session, "
                      f"{oh_stats[0]} sidestepped; known for char {fchar}: "
                      f"{sorted(oh_seen.get(str(fchar), ()))}")
            if throw_cmd:
                print("  throws seen this session (startup move: cmd, h/m/l, class):")
                for tmv, (c, h) in sorted(throw_cmd.items()):
                    print(f"    {tmv:<6} cmd {c:<5} h/m/l {h}  {throw_class(c, h)}")
            print(f"  {ducked} throws stepped, {punished} punishes thrown "
                  f"({by_kind.get('punish', [0, 0])[0]} landed), "
                  f"{approach_hits[0]} run-ups attacked "
                  f"({by_kind.get('approach', [0, 0])[0]} landed), "
                  f"{pokes[0]} pokes ({by_kind.get('poke', [0, 0])[0]} landed)")
        if dmg_by_move:
            total = sum(dmg_by_move.values())
            print(f"  OUR damage taken: {total} total. By their move id:")
            for m, d in sorted(dmg_by_move.items(), key=lambda kv: -kv[1])[:8]:
                print(f"    move={m:<6} {d:>4}  ({100 * d / total:.0f}%)")
            print("  by what we had just done:")
            for a, d in sorted(dmg_by_state.items(), key=lambda kv: -kv[1]):
                print(f"    {a:<16} {d:>4}  ({100 * d / total:.0f}%)")
        print(f"  Break Gauge seen: ours {gauge_seen['me'][0]}..{gauge_seen['me'][1]}, "
              f"theirs {gauge_seen['foe'][0]}..{gauge_seen['foe'][1]}  "
              f"(--gauge-max {args.gauge_max}); {breaks[0]} break holds, "
              f"{breaks[1]} break blows")
        if stun_tab:
            rows = sorted(stun_tab.items(), key=lambda kv: -kv[1][1])
            print("  holds tried while stunned, by our stun animation id (landed/tried; "
                  "0/3+ = retired):")
            print("    " + "  ".join(f"{k} {ok}/{n}" for k, (ok, n) in rows[:12]))
        if pre_stats:
            print("  by OUR move id at the moment of input (0 = standing idle):")
            for k, (ok, n) in sorted(pre_stats.items(), key=lambda kv: -kv[1][1]):
                print(f"    pre={str(k):<6} {ok:>3}/{n:<3}")
        if facing_fixes[0]:
            print(f"  facing probe in the hold saw no walk id in time {oracle_miss[0]}x; "
                  f"zoning walk flipped the facing {zone_flip[1]}x; "
                  f"4S needed gauge est >= {break_need[0]} by the end")
            print(f"  {probe_dirty_n[0]} probes settled the facing right after a side swap "
                  f"(hold throw / thrown / new opponent / round start); "
                  f"{swap_flips[0]} swaps seen in the positions across a throw / knockdown")
            print(f"  {facing_fixes[0]} holds had their facing corrected by the "
                  f"pretap probe (the X-sign guess was wrong)")
        if keys_held_count[0]:
            print(f"  [!] {keys_held_count[0]} holds were fired while a bound "
                  f"key was ALREADY DOWN (a hand on the keyboard, or a pad) - "
                  f"those cannot come out as holds")
        if my_anim:
            print("  what OUR character was doing 120ms after the input -")
            print("  152/154/155/156 = the hold came out and whiffed (7H/4H/"
                  "6H/1H), 0 = it never came out, other ids = it caught:")
            for kind in ("high", "midp", "midk", "low", "break", "jab", "punish", "approach", "poke"):
                for ok in (True, False):
                    d = my_anim.get((kind, ok))
                    if not d:
                        continue
                    top = sorted(d.items(), key=lambda kv: -kv[1])[:6]
                    print(f"    {kind:<5} {'landed' if ok else 'failed':<7} "
                          + "  ".join(f"{m}x{c}" for m, c in top))


if __name__ == "__main__":
    main()
