"""One place where a combo token becomes key presses.

holdbot and comboreplay both send strings like "66P", "3K" or "214T". For a
long time each had its own copy of the recipe and the two drifted: the dash
run-up, the down-diagonal ordering, the press length and the string direction
lead were all found once in the replay and then had to be found again, one at a
time, in the match engine. Everything lives here now.

What the recipes encode, all of it measured against DOA6LR:

  motion     "236P" taps every direction but the last for 2 frames each.
  dash       "66P" is a RUN and then a punch: the run-up is most of its range,
             and under ~0.13 s the game does not read a dash at all - 66P comes
             out as a plain 6P.
  down       A down diagonal needs BOTH directions in place before the button.
             Sent alongside it the game keeps only the horizontal: 3K came out
             as 6K, 1P and 7P as 4P, while the up diagonals were fine.
  vertical   Otherwise the vertical goes down WITH the button. On its own a
             frame early it is a sidestep and the button comes out neutral.
  string     A direction inside a string needs ~50 ms, not the 17 ms that works
             from neutral: PPP>4P came out as the plain fourth P.
  press      45 ms on the button. 20 ms (1.2 frames at 60 fps) is missed inside
             a string, which is what made follow-ups vanish.
  stance     A token with no button at all is a stance entry (Minato's PPP4):
             hold the direction through the previous move's recovery.
"""
import re
import time

from pad import dirs_to_names

BUTTON_KEY = {"P": "punch", "K": "kick", "PK": "pk", "HK": "hk", "S": "special",
              "T": "throw", "H": "free"}
NUMPAD = {1: (-1, -1), 2: (0, -1), 3: (1, -1), 4: (-1, 0), 5: (0, 0),
          6: (1, 0), 7: (-1, 1), 8: (0, 1), 9: (1, 1)}

DASH_MIN = 0.13          # under this the game does not see a dash
DASH_MAX = 0.35
PRESS = 0.045            # button hold
DIR_LEAD = 0.017         # horizontal ahead of the button, from neutral
DIR_LEAD_STRING = 0.05   # ...and inside a string
STANCE_HOLD = 0.18       # a bare direction, entering a stance


def split_token(tok):
    """'46PK' -> ([4, 6], 'PK'); 'P' -> ([], 'P'); '4' -> ([4], '')."""
    m = re.match(r"(\d*)([A-Z+]*)", tok.strip().upper())
    if not m:
        return [], ""
    return [int(ch) for ch in m.group(1)], m.group(2).replace("+", "")


def dash_run(distance=None):
    """How long to run before a dash attack. The run-up is the dash's reach,
    so it scales with the gap - but never below the floor, or it stops being
    a dash."""
    if not distance:
        return DASH_MIN
    return max(DASH_MIN, min(DASH_MAX, distance / 1100.0))


def press_token(inj, tok, facing_right=True, hold=PRESS, distance=None,
                in_string=False, held=False):
    """Send one combo token. Returns False if the token means nothing."""
    digits, btn = split_token(tok)
    if btn and btn not in BUTTON_KEY:
        return False

    if not btn:                       # a bare direction: a stance entry
        if not digits:
            return False
        dx, dy = NUMPAD.get(digits[-1], (0, 0))
        if not facing_right:
            dx = -dx
        names = dirs_to_names(dx, dy)
        if not names:
            return False
        inj.down(names)
        time.sleep(STANCE_HOLD)
        inj.up(names)
        return True

    key = BUTTON_KEY[btn]
    for dg in digits[:-1]:            # the motion, every direction but the last
        mdx, mdy = NUMPAD.get(dg, (0, 0))
        if not facing_right:
            mdx = -mdx
        mn = dirs_to_names(mdx, mdy)
        if mn:
            inj.down(mn)
            time.sleep(0.033)
            inj.up(mn)
            time.sleep(0.017)

    dx, dy = NUMPAD.get(digits[-1], (0, 0)) if digits else (0, 0)
    if not facing_right:
        dx = -dx
    horiz, vert = dirs_to_names(dx, 0), dirs_to_names(0, dy)
    dash = len(digits) >= 2 and digits[-1] == digits[-2]

    if dash and horiz:
        inj.down(horiz)
        time.sleep(dash_run(distance))
        inj.down(vert + [key])
    elif horiz and vert and dy < 0:
        inj.down(horiz + vert)
        time.sleep(0.05)
        inj.down([key])
    else:
        if horiz:
            inj.down(horiz)
            time.sleep(DIR_LEAD_STRING if in_string else DIR_LEAD)
        inj.down(vert + [key])
    time.sleep(0.7 if held else hold)
    inj.up([key])
    inj.up(vert + horiz)
    return True
