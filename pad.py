"""
pad.py - input injection, with two interchangeable backends.

  vgamepad   a virtual Xbox 360 pad via ViGEmBus. The game cannot tell it from
             real hardware, so this is the one to prefer.
             pip install vgamepad   (installs the ViGEmBus driver on first run)

  keyboard   SendInput with hardware scancodes. No driver needed, works when
             the game is bound to the keyboard, but some games ignore
             synthesised keys depending on how they read input.

Both expose the same interface, so holdbot.py does not care which is in use:

    inj = make_injector("vgamepad")
    inj.tap(dx=-1, dy=-1, buttons=["free"], frames=3)

Directions are given in screen space (dx=-1 is left). holdbot mirrors them for
you based on which side your character is standing on.
"""

import ctypes
import ctypes.wintypes as wt
import time

FRAME = 1.0 / 60.0

_winmm = ctypes.WinDLL("winmm")


def enable_high_res_timer():
    """Raise the system timer resolution to 1ms for the life of the process.

    Windows defaults to a 15.6ms scheduling tick, so time.sleep(0.002) really
    sleeps ~15ms and time.sleep(0.020) really sleeps ~31ms. That turns a
    500 Hz poll loop into a 60 Hz one and doubles input latency - about four
    frames, which is most of a DOA5 startup window. Measured on this machine:
    2ms -> 15.24ms and 20ms -> 31.00ms before, 2.34ms and 20.51ms after.
    """
    return _winmm.timeBeginPeriod(1) == 0


def disable_high_res_timer():
    _winmm.timeEndPeriod(1)

# The four-point hold, as (dx, dy) facing-relative, dx=-1 meaning "back".
# DOA6 keeps DOA5's table: 7H high, 4H mid punch, 6H mid kick, 1H low
# (fightinggameguide.com/doa6: "4H Holds mid punches and 6H Holds mid kicks").
# Mid kick is held FORWARD, which is the counter-intuitive one and the reason
# a guessed table gets two of the four wrong.
HOLD_DIRECTIONS = {
    "high":  (-1, +1),   # 7  back-up     beats high punch and high kick
    "midp":  (-1,  0),   # 4  back        beats mid punch
    # DOA6: forward + H came out as a plain GUARD (move 270) in two of three
    # neutral tests, and 6H never caught a mid kick in nine live attempts.
    # Back + H produced two animation ids (154 / 155) under one command
    # code (168), so the working hypothesis is DOA6's regular hold is
    # three-point - 4H covers both mids - and the game picks the variant.
    # Override with --hold midk=6 to test the four-point reading again.
    "midk":  (-1,  0),   # 4  back        (DOA5 used 6, see above)
    "low":   (-1, -1),   # 1  back-down   beats low punch and low kick
}

# StrikeType (read straight out of the game) -> which hold beats it.
STRIKE_TO_HOLD = {0: "high", 1: "high", 2: "midp",
                  3: "midk", 4: "low", 5: "low"}

# Numpad notation -> (dx, dy), facing-relative, so a hold can be re-specified
# without translating directions by hand. Not every character uses the standard
# four-point set: some have a three-point hold where one input covers both mid
# punch and mid kick, and against those 6H simply never comes out.
NUMPAD = {
    7: (-1, +1), 8: (0, +1), 9: (+1, +1),
    4: (-1,  0), 5: (0,  0), 6: (+1,  0),
    1: (-1, -1), 2: (0, -1), 3: (+1, -1),
}


def parse_hold_overrides(spec, base=None):
    """Turn "midk=4,low=2" into a HOLD_DIRECTIONS mapping."""
    table = dict(base or HOLD_DIRECTIONS)
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, num = part.partition("=")
        name = name.strip()
        if name not in table:
            raise ValueError(f"unknown hold {name!r}; "
                             f"expected one of {sorted(table)}")
        try:
            key = int(num)
        except ValueError:
            raise ValueError(f"{name}: expected a numpad digit, got {num!r}")
        if key not in NUMPAD:
            raise ValueError(f"{name}: {key} is not a numpad direction 1-9")
        table[name] = NUMPAD[key]
    return table


# ------------------------------------------------------------ keyboard impl

u32 = ctypes.WinDLL("user32", use_last_error=True)

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1

SCAN = {
    # extended keys (arrow cluster) need KEYEVENTF_EXTENDEDKEY
    "up": (0xC8, True), "down": (0xD0, True),
    "left": (0xCB, True), "right": (0xCD, True),
    # letters, set 1 scancodes
    "a": (0x1E, False), "b": (0x30, False), "c": (0x2E, False),
    "d": (0x20, False), "e": (0x12, False), "f": (0x21, False),
    "g": (0x22, False), "h": (0x23, False), "i": (0x17, False),
    "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
    "m": (0x32, False), "n": (0x31, False), "o": (0x18, False),
    "p": (0x19, False), "q": (0x10, False), "r": (0x13, False),
    "s": (0x1F, False), "t": (0x14, False), "u": (0x16, False),
    "v": (0x2F, False), "w": (0x11, False), "x": (0x2D, False),
    "y": (0x15, False), "z": (0x2C, False),
    # digits
    "1": (0x02, False), "2": (0x03, False), "3": (0x04, False),
    "4": (0x05, False), "5": (0x06, False), "6": (0x07, False),
    "7": (0x08, False), "8": (0x09, False), "9": (0x0A, False),
    "0": (0x0B, False),
    # numpad
    "num0": (0x52, False), "num1": (0x4F, False), "num2": (0x50, False),
    "num3": (0x51, False), "num4": (0x4B, False), "num5": (0x4C, False),
    "num6": (0x4D, False), "num7": (0x47, False), "num8": (0x48, False),
    "num9": (0x49, False),
    # modifiers and friends
    "space": (0x39, False), "lshift": (0x2A, False), "lctrl": (0x1D, False),
    "lalt": (0x38, False), "rshift": (0x36, False), "tab": (0x0F, False),
    "comma": (0x33, False), "period": (0x34, False), "slash": (0x35, False),
    "semicolon": (0x27, False), "quote": (0x28, False),
    "lbracket": (0x1A, False), "rbracket": (0x1B, False),
    "minus": (0x0C, False), "equals": (0x0D, False), "backslash": (0x2B, False),
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _IU(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _IU)]


u32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wt.UINT


class KeyboardInjector:
    """Maps abstract directions/buttons to keys, then drives SendInput."""

    def __init__(self, binds=None):
        # DOA6's default keyboard layout is the same as DOA5LR's:
        #   P = K   K = L   H (Guard/Hold) = J   Throw = M
        #   P+K = U   H+K = O   S (Special: Fatal Rush / Break Hold 4S /
        #   Break Blow 6S) = I   Taunt = N
        # `free` is the Hold button and is the one that matters here. Binding
        # it to L instead - the Kick button - is not a silent failure: the bot
        # cheerfully throws a kick on every read, the opponent takes damage,
        # and a health-based success check reports it as a landed hold.
        self.binds = binds or {
            "up": "up", "down": "down", "left": "left", "right": "right",
            "punch": "k", "kick": "l", "free": "j", "throw": "m",
            # DOA6: S (Special) = I. 4S with >= 50% Break Gauge is the
            # Break Hold (catches any height), 6S at 100% the Break Blow.
            "special": "i", "pk": "u", "hk": "o",
        }
        self.held = set()

    def _send(self, keys, up):
        """Send every key in one SendInput call.

        One call per key lets the game sample between them, so a diagonal can
        arrive as two separate cardinal directions. That matters here: DOA5's
        high (7) and low (1) holds are diagonals, and they landed far less
        often than the pure horizontal 4 and 6 until these became atomic.
        """
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return
        arr = (INPUT * len(keys))()
        for i, key in enumerate(keys):
            scan, ext = SCAN[key]
            flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
            if ext:
                flags |= KEYEVENTF_EXTENDEDKEY
            arr[i].type = INPUT_KEYBOARD
            arr[i].ki = KEYBDINPUT(0, scan, flags, 0, None)
        u32.SendInput(len(keys), arr, ctypes.sizeof(INPUT))

    def down(self, names):
        keys = [self.binds[n] for n in names if self.binds[n] not in self.held]
        self.held.update(keys)
        self._send(keys, False)

    def up(self, names):
        keys = [self.binds[n] for n in names if self.binds[n] in self.held]
        self.held.difference_update(keys)
        self._send(keys, True)

    def release_all(self):
        keys = list(self.held)
        self.held.clear()
        self._send(keys, True)

    def tap_key(self, key, duration=0.060):
        """Press one raw key by name, bypassing the action bindings.

        Used by inputtest.py to ask the game what each physical key does,
        rather than trusting a guessed binding table.
        """
        self._send(key, False)
        time.sleep(duration)
        self._send(key, True)

    def close(self):
        self.release_all()


# ------------------------------------------------------------ vigem impl

class VGamepadInjector:
    """Virtual Xbox 360 pad. The game sees a real XInput device."""

    def __init__(self):
        import vgamepad as vg
        self.vg = vg
        self.pad = vg.VX360Gamepad()
        self.btn = {
            "punch": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            "kick":  vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            "free":  vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            "throw": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            "up":    vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            "down":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            "left":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
            "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        }
        self.held = set()

    def down(self, names):
        for n in names:
            if n not in self.held:
                self.held.add(n)
                self.pad.press_button(self.btn[n])
        self.pad.update()

    def up(self, names):
        for n in names:
            if n in self.held:
                self.held.discard(n)
                self.pad.release_button(self.btn[n])
        self.pad.update()

    def release_all(self):
        self.pad.reset()
        self.pad.update()
        self.held.clear()

    def close(self):
        self.release_all()


class NullInjector:
    """Dry-run backend: logs intent, touches nothing."""

    def __init__(self):
        self.held = set()

    def down(self, names):
        self.held |= set(names)

    def up(self, names):
        self.held -= set(names)

    def release_all(self):
        self.held.clear()

    def close(self):
        pass


# ---------------------------------------------------------------- frontend

def make_injector(kind="vgamepad", binds=None):
    if kind == "vgamepad":
        try:
            return VGamepadInjector()
        except Exception as e:
            print(f"  vgamepad unavailable ({e}); falling back to keyboard.\n"
                  f"  install with: pip install vgamepad")
            return KeyboardInjector(binds)
    if kind == "keyboard":
        return KeyboardInjector(binds)
    return NullInjector()


def dirs_to_names(dx, dy):
    names = []
    if dx < 0:
        names.append("left")
    elif dx > 0:
        names.append("right")
    if dy > 0:
        names.append("up")
    elif dy < 0:
        names.append("down")
    return names


def tap(inj, dx=0, dy=0, buttons=(), frames=3):
    """Press a direction plus buttons for `frames`, then release.

    Direction goes down one frame early: fighting games sample the stick before
    the button, and a same-instant press can register as a bare button.
    """
    names = dirs_to_names(dx, dy)
    inj.down(names)
    time.sleep(FRAME)
    inj.down(list(buttons))
    time.sleep(FRAME * max(frames - 1, 1))
    inj.up(list(buttons))
    inj.up(names)


def hold(inj, kind, facing_right=True, pretap=0.020, press=0.020,
         dir_lead=0.0, table=None):
    """Perform one hold for the given attack class.

    Returns the timestamp at which the Hold button went down, which is the
    moment the game actually samples - the release afterwards costs latency in
    the caller's wall clock but not in whether the input landed.

    The opposite direction is tapped for a moment first. DOA5 reads the stick
    at the instant Hold is pressed, and a direction already held from walking
    or guarding can be swallowed; the tap forces a fresh transition. This is
    lifted from WAZAAAAA's BusaBot, which needed it to hold reliably.
    """
    dx, dy = (table or HOLD_DIRECTIONS)[kind]
    if not facing_right:
        dx = -dx                      # numpad is facing-relative, keys are not
    if pretap and dx:
        opp = dirs_to_names(-dx, 0)
        inj.down(opp)
        time.sleep(pretap)
        inj.up(opp)
    names = dirs_to_names(dx, dy)
    if dir_lead:
        # Give the game a frame to sample the direction before Hold arrives.
        # Costs latency, and on DOA6 it is the wrong idea: see below.
        inj.down(names)
        time.sleep(dir_lead)
        inj.down(["free"])
    else:
        # Direction AND Hold in ONE SendInput call. DOA6 samples the stick
        # the instant it sees a direction: if "down" arrives even one call
        # ahead of H, the character starts crouching and H becomes a crouch
        # guard - measured as 1H coming out as movement ids 13/87/92 in five
        # of eight attempts, while 4H/6H (no vertical component) were fine.
        inj.down(names + ["free"])
    pressed_at = time.perf_counter()
    time.sleep(press)
    inj.up(["free"])
    inj.up(names)
    return pressed_at


def guard(inj, crouch=False, facing_right=True, press=0.200, frames=None):
    """Block instead of countering.

    The fallback for attacks a hold cannot beat. Free on its own puts the
    character in a guard stance - inputtest measured move 284, in place, no
    drift - and a low attack needs that crouched, hence the down direction.

    Do NOT add back to this. Back plus Free is 4H, the mid punch hold, so a
    "guard" built that way whiffs against anything but a mid punch and leaves
    the character open. That mistake measured 37 broken guards out of 39
    against a single mid kick, every one of them ending in hit stun.

    A fixed duration is the wrong default: the guard has to still be up when
    the attack lands, and that is `frames` away, not some constant. Holding
    200ms against an attack 13 frames (217ms) out drops the block a moment
    before impact - which looks exactly like the block not working at all.

    Returns the timestamp the guard went up.
    """
    if frames is not None:
        press = max(press, (frames + 6) * FRAME)
    names = ["down"] if crouch else []
    inj.down(names)
    inj.down(["free"])
    up_at = time.perf_counter()
    time.sleep(press)
    inj.up(["free"])
    inj.up(names)
    return up_at


def strike(inj, button="punch", press=0.020):
    """Throw out a neutral attack. Strikes beat throws in DOA5's triangle, so
    this is the answer to a throw startup - a hold would simply be caught."""
    inj.down([button])
    pressed_at = time.perf_counter()
    time.sleep(press)
    inj.up([button])
    return pressed_at


if __name__ == "__main__":
    import sys
    kind = sys.argv[1] if len(sys.argv) > 1 else "vgamepad"
    inj = make_injector(kind)
    print(f"backend: {type(inj).__name__}")
    print("cycling the four holds, 1.5s apart. Focus the game now.\n"
          "Watch which way your character turns; that tells you whether the\n"
          "direction mapping matches your pad.")
    time.sleep(3)
    for name in ("high", "midp", "midk", "low"):
        dx, dy = HOLD_DIRECTIONS[name]
        print(f"  {name:<5} dx={dx:+d} dy={dy:+d} + free")
        hold(inj, name, facing_right=True)
        time.sleep(1.5)
    inj.close()
