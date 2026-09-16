"""
scanner.py - hotkey-driven differential memory scanner, tuned for finding
"what state is this character in right now" fields (move id, stance, attack
property) rather than plain numbers like HP.

Run it, tab back into the game, and drive it entirely from the F-keys. Every
action beeps so you never have to look at the console mid-search.

  F1   set reference   capture current memory as the baseline, reset candidates
                       (DOA6LR: ~1.7 GB after skipping GPU heaps and zero
                       pages, 2-4 s; keep the game windowed so alt-tab is
                       cheap)
  F2   CHANGED         value differs from the REFERENCE
  F3   SAME            value equals the REFERENCE
  F8   MOVING          value IS changing right now
  F9   STILL           value is NOT changing right now
  F11  DROPPED         value fell since the previous step  (health bars)
  F12  ROSE            value rose since the previous step
  F4   undo            step back one filter
  F5   record          sample candidates at ~60 Hz until pressed again
  F6   report          rank candidates and print them (also clusters structs)
  F7   save            write candidates + report to scan_result.json
  F10  quit

Two different chains, for two different kinds of field.

1. Discrete state (stance, move id) - "changed, then came back":

     stand neutral   F1        (reference = neutral)
     hold crouch     F2        (changed while crouching)
     release         F3        (and changed back when neutral again)
     ... alternate F2/F3 until the count stops dropping

   Kills animation interpolators, timers, RNG and particle state, which no
   single snapshot comparison can do.

2. Continuous state (world position) - "moving, then still". Position never
   returns to an old value, so chain 1 cannot find it. F8/F9 instead ask what
   a value is doing at the moment you press them, each taking its own burst of
   samples, so the answer does not depend on your key timing:

     stand still     F1, then F2   (seed the candidate set)
     KEEP WALKING    F8        (hold the direction while it samples)
     stand still     F9        (let go first, then press)
     ... alternate F8/F9

   F9 is the one that matters: bone and animation data keeps churning through
   the idle animation, but the root position freezes the instant you stop, so
   one F9 removes the skeleton arrays that otherwise dominate the results.
   Each press samples for ~0.3s, so hold the state until it beeps.
"""

import argparse
import ctypes
import json
import sys
import time

import numpy as np

from memlib import Process, find_pid

u32 = ctypes.WinDLL("user32", use_last_error=True)

VK = {"F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
      "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
      "F11": 0x7A, "F12": 0x7B}

# Numpad 1-9 pick which label the next recording session belongs to. The
# numpad is used rather than the number row because the game leaves it unbound.
for _i in range(1, 10):
    VK[f"NUM{_i}"] = 0x60 + _i

DTYPES = {"u8": np.uint8, "u16": np.uint16, "u32": np.uint32,
          "i32": np.int32, "f32": np.float32}

RAW = {1: np.uint8, 2: np.uint16, 4: np.uint32}


def raw(arr):
    """Bit-pattern view, so float compares are exact and NaN == NaN.

    Comparing float32 directly would make every NaN slot look like it changed
    on every single step, which quietly poisons a f32 position scan.
    """
    return arr.view(RAW[arr.dtype.itemsize])


def beep(freq, ms):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


class Keys:
    """Edge-triggered global hotkeys via polling, so the game keeps focus."""

    def __init__(self):
        self.down = set()

    def pressed(self):
        hits = []
        for name, vk in VK.items():
            is_down = bool(u32.GetAsyncKeyState(vk) & 0x8000)
            if is_down and name not in self.down:
                hits.append(name)
            if is_down:
                self.down.add(name)
            else:
                self.down.discard(name)
        return hits


# --------------------------------------------------------------- memory io

def read_values(proc, addrs, dt, max_span=1 << 16):
    """Read one `dt` value at each address. Addresses must be sorted."""
    item = np.dtype(dt).itemsize
    out = np.zeros(len(addrs), dtype=dt)
    n = len(addrs)
    i = 0
    while i < n:
        start = int(addrs[i])
        j = i
        while j + 1 < n and int(addrs[j + 1]) + item - start <= max_span:
            j += 1
        end = int(addrs[j]) + item
        blob = np.frombuffer(proc.read_tolerant(start, end - start),
                             dtype=np.uint8)
        offs = (addrs[i:j + 1].astype(np.int64) - start)
        idx = offs[:, None] + np.arange(item)[None, :]
        gathered = np.ascontiguousarray(blob[idx])
        out[i:j + 1] = gathered.view(dt).ravel()
        i = j + 1
    return out


CHUNK = 64 << 20     # read big regions in 64 MB pieces


def snapshot_regions(proc, dt, chunk=CHUNK, skip_zero=True, times=None):
    """Full read of every scannable region, as {(base, size): array}.

    Regions are read in `chunk`-byte pieces so the 4.2 GB arena DOA6LR keeps
    never needs a 4 GB buffer. Pieces that are entirely zero are dropped when
    skip_zero is set: roughly three quarters of that arena is untouched, and a
    live struct always has non-zero neighbours inside the same 64 MB.

    `times`, if given, receives {(base, size): perf_counter at read} so a
    caller can reason about how much wall-clock passed between two snapshots
    of the same piece - a snapshot takes seconds and a frame counter advances
    the whole time, so per-piece timing is what makes a rate filter exact.
    """
    item = np.dtype(dt).itemsize
    snap = {}
    for r in proc.regions(chunk=chunk):
        count = r.size // item
        if count == 0:
            continue
        t = time.perf_counter()
        buf = proc.read_tolerant(r.base, count * item)
        arr = np.frombuffer(buf, dtype=dt, count=count)
        if skip_zero and not arr.any():
            continue
        snap[(r.base, r.size)] = arr
        if times is not None:
            times[(r.base, r.size)] = t
    return snap


# ----------------------------------------------------------------- scoring

def score_timeline(series):
    """Rank how much a value behaves like a discrete state field.

    series: (frames, candidates) array. Returns (score, distinct, switches).
    Wants few distinct values, long flat runs, and no monotonic drift.
    """
    frames = series.shape[0]
    f64 = series.astype(np.float64)

    changes = (f64[1:] != f64[:-1])
    switches = changes.sum(axis=0)

    distinct = np.array([len(np.unique(f64[:, i]))
                         for i in range(f64.shape[1])], dtype=np.float64)

    deltas = np.diff(f64, axis=0)
    ups = (deltas > 0).sum(axis=0)
    downs = (deltas < 0).sum(axis=0)
    monotonic = (np.minimum(ups, downs) == 0) & (switches > 2)

    # a state field: switches rarely, revisits a small set of values
    flat = 1.0 - (switches / max(frames - 1, 1))
    variety = np.clip(distinct, 1, None)
    revisit = switches / variety           # >1 means values recur, not a ramp

    score = flat * np.clip(revisit, 0, 8) / 8.0
    score[distinct < 2] = 0.0              # never moved: not a state
    score[distinct > frames * 0.5] = 0.0   # basically continuous
    score[monotonic] = 0.0                 # a counter, not a state
    return score, distinct.astype(int), switches.astype(int)


def analyse_labels(sessions, addrs, label_names, max_exclusive=6,
                   max_switch=0.25):
    """Find candidates whose values are exclusive to one label AND hold steady.

    sessions: {label: (frames, ncand) array}. A field that separates the labels
    holds a value under one label that never appears under any other. Frames
    where nothing is happening are common to every session, so idle state
    cancels out on its own and never has to be excluded by hand.

    Exclusivity alone is far too weak here. Different attack types are played
    by different animations, so every interpolated animation byte separates the
    labels trivially and floods the result. What sets a state field apart is
    that it is piecewise constant: it holds one value for a whole move and
    jumps only at move boundaries, while animation data moves nearly every
    frame. max_switch is that test, and it also acts as a cheap prefilter so
    the expensive set analysis only runs on plausible columns.
    """
    labs = [l for l in label_names if sessions.get(l) is not None
            and sessions[l].shape[0] >= 20]
    if len(labs) < 2:
        return [], labs
    ncand = min(sessions[l].shape[1] for l in labs)

    rate = np.zeros(ncand)
    for l in labs:
        a = sessions[l][:, :ncand]
        np.maximum(rate, (a[1:] != a[:-1]).mean(axis=0), out=rate)
    steady = np.flatnonzero((rate <= max_switch) & (rate > 0))
    if steady.size == 0:
        return [], labs

    uniq = {l: {c: np.unique(sessions[l][:, c]) for c in steady} for l in labs}

    findings = []
    for c in steady:
        sets = {l: set(uniq[l][c].tolist()) for l in labs}
        excl, ok = {}, True
        for l in labs:
            others = set()
            for o in labs:
                if o != l:
                    others |= sets[o]
            own = sets[l] - others
            if not own or len(own) > max_exclusive:
                ok = False
                break
            excl[l] = sorted(own)
        if not ok:
            continue
        spread = sum(len(sets[l]) for l in labs) / len(labs)
        score = 1.0 / (1.0 + sum(len(v) for v in excl.values()) - len(labs)) \
            / (1.0 + spread / 16.0) * (1.0 - rate[c] / max(max_switch, 1e-9))
        findings.append({"address": int(addrs[c]), "score": round(float(score), 4),
                         "switch_rate": round(float(rate[c]), 4),
                         "values": {l: [float(x) for x in excl[l]] for l in labs}})
    findings.sort(key=lambda f: -f["score"])
    return findings, labs


def cluster(addrs, gap=0x1000):
    """Group addresses into runs separated by more than `gap`; struct hints."""
    if len(addrs) == 0:
        return []
    groups, start, prev = [], int(addrs[0]), int(addrs[0])
    count = 1
    for a in addrs[1:]:
        a = int(a)
        if a - prev > gap:
            groups.append((start, prev, count))
            start, count = a, 0
        prev = a
        count += 1
    groups.append((start, prev, count))
    return groups


# -------------------------------------------------------------------- main

class Scanner:
    def __init__(self, proc, dt, name):
        self.p = proc
        self.dt = dt
        self.name = name
        self.base_snap = None      # {(base,size): array} full reference
        self.base_times = {}       # {(base,size): perf_counter at capture}
        self.last_time = None      # when self.last was read
        self.addrs = None          # np array of candidate addresses
        self.ref = None            # values at F1 reference time
        self.last = None           # values at the previous filter step
        self.history = []          # (addrs, ref, last) stack for undo
        self.steps = 0
        self.series = None
        self.recording = False
        self.rec_buf = []
        self.labels = []           # label names, from --labels
        self.sessions = {}         # {label: (frames, ncand) array}
        self.active_label = None
        self.findings = []
        self.max_exclusive = 6
        self.max_switch = 0.25

    # -- filters ----------------------------------------------------------

    def set_reference(self):
        # F1 wipes the candidate set. Pressing it once per round instead of
        # once per session is the easy mistake, and it silently caps the
        # search at two filters, so say so loudly.
        if self.addrs is not None:
            print(f"  [!] F1 DISCARDED {len(self.addrs)} narrowed candidates "
                  f"and started over.")
            print(f"  [!] Press F1 only ONCE per session, then alternate "
                  f"F2/F3 to keep narrowing.")
            beep(400, 250)
        t = time.perf_counter()
        self.base_times = {}
        self.base_snap = snapshot_regions(self.p, self.dt,
                                          times=self.base_times)
        self.addrs = None
        self.ref = None
        self.last = None
        self.history.clear()
        self.steps = 0
        mb = sum(a.nbytes for a in self.base_snap.values()) / 1048576
        print(f"[F1] reference captured: {len(self.base_snap)} regions, "
              f"{mb:.0f} MB, {time.perf_counter() - t:.2f}s")
        beep(900, 90)

    def _first_filter(self, compare, timed=False):
        """Compare live memory against the reference, piece by piece.

        With timed=True the comparator also receives the seconds elapsed
        since that piece's reference read, for rate-based tests.
        """
        item = np.dtype(self.dt).itemsize
        keep_a, keep_ref, keep_now = [], [], []
        for (base, size), old in self.base_snap.items():
            t = time.perf_counter()
            buf = self.p.read_tolerant(base, old.size * item)
            new = np.frombuffer(buf, dtype=self.dt, count=old.size)
            if timed:
                m = compare(new, old, t - self.base_times.get((base, size), t))
            else:
                m = compare(new, old)
            idx = np.flatnonzero(m)
            if idx.size:
                keep_a.append(base + idx.astype(np.uint64) * item)
                keep_ref.append(old[idx])
                keep_now.append(new[idx])
        if not keep_a:
            z = np.zeros(0, self.dt)
            return np.zeros(0, np.uint64), z, z
        a = np.concatenate(keep_a)
        order = np.argsort(a)
        return (a[order], np.concatenate(keep_ref)[order],
                np.concatenate(keep_now)[order])

    def filter(self, keep_changed):
        """Compare the current value against the F1 reference."""
        if self.base_snap is None:
            print("  press F1 first to set a reference")
            beep(300, 200)
            return
        cmp = ((lambda n, o: raw(n) != raw(o)) if keep_changed
               else (lambda n, o: raw(n) == raw(o)))
        t = time.perf_counter()
        if self.addrs is None:
            self.addrs, self.ref, self.last = self._first_filter(cmp)
        else:
            self._push_undo()
            now = read_values(self.p, self.addrs, self.dt)
            self._keep(cmp(now, self.ref), now)
        self._report_step("CHANGED" if keep_changed else "SAME", t)
        beep(1200 if keep_changed else 700, 90)

    def filter_delta(self, less):
        """Keep values that dropped (or rose) since the previous step.

        The classic way to pin a health bar: every hit narrows the set, and
        nothing else in memory happens to fall on exactly the same schedule.
        Unsigned compare also works for positive IEEE-754 floats, so a u32 scan
        finds both an integer and a float health field.
        """
        if self.base_snap is None:
            print("  press F1 first to set a reference")
            beep(300, 200)
            return
        cmp = (lambda n, o: n < o) if less else (lambda n, o: n > o)
        t = time.perf_counter()
        if self.addrs is None:
            self.addrs, self.ref, self.last = self._first_filter(cmp)
        else:
            self._push_undo()
            now = read_values(self.p, self.addrs, self.dt)
            self._keep(cmp(now, self.last), now)
        self._report_step("DROPPED" if less else "ROSE", t)
        beep(1000 if less else 1400, 90)

    def filter_range(self, lo, hi, tag=None):
        """Keep candidates whose current value lies in [lo, hi].

        Works as a first filter too, straight off the reference snapshot.
        Cheap and exact, so it is the tool for "the counter must have just
        reset" or "health is somewhere around 300".
        """
        if self.base_snap is None:
            print("  press F1 first to set a reference")
            beep(300, 200)
            return
        t = time.perf_counter()
        cmp = lambda n, o: (n >= lo) & (n <= hi)
        if self.addrs is None:
            self.addrs, self.ref, self.last = self._first_filter(cmp)
        else:
            self._push_undo()
            now = read_values(self.p, self.addrs, self.dt)
            self._keep(cmp(now, self.last), now)
        self.last_time = time.perf_counter()
        self._report_step(tag or f"IN[{lo},{hi}]", t)

    def filter_rate(self, per_second, tol, tag="RATE"):
        """Keep values that advanced by about per_second * elapsed since the
        previous step (or the reference, on the first pass).

        This is how a frame counter is found without any timing-critical
        input: it ticks 60 times a second whether or not anyone presses a
        key, and almost nothing else in memory advances at exactly that rate.
        The elapsed time is measured per piece on the first pass, because a
        full snapshot takes seconds and the counter keeps running throughout.
        """
        if self.base_snap is None:
            print("  press F1 first to set a reference")
            beep(300, 200)
            return
        t = time.perf_counter()
        wide = (np.float64 if np.dtype(self.dt).kind == "f" else np.int64)
        if self.addrs is None:
            def cmp(n, o, dt):
                want = per_second * dt
                d = n.astype(wide)
                d -= o.astype(wide)
                return np.abs(d - want) <= tol + 0.05 * want
            self.addrs, self.ref, self.last = self._first_filter(cmp, timed=True)
            # The values just kept were read over the whole pass - many
            # seconds apart from piece to piece - so they are useless as a
            # baseline for the next rate test. Re-read the (now small) set
            # in one go so `last` and `last_time` describe one instant.
            self.refresh_last()
        else:
            self._push_undo()
            now = read_values(self.p, self.addrs, self.dt)
            elapsed = time.perf_counter() - (self.last_time or t)
            want = per_second * elapsed
            d = now.astype(wide) - self.last.astype(wide)
            self._keep(np.abs(d - want) <= tol + 0.05 * want, now)
        self._report_step(tag, t)

    def refresh_last(self):
        """Re-read the candidates so the next delta filter starts from now."""
        if self.addrs is not None and len(self.addrs):
            self.last = read_values(self.p, self.addrs, self.dt)
            self.last_time = time.perf_counter()

    def filter_activity(self, must_move, samples=5, span=0.30):
        """Keep values that are (or are not) changing *right now*.

        Self-contained: it takes its own burst of samples when pressed, so the
        verdict does not depend on when the previous key was pressed. An
        earlier design compared against the previous filter step and was
        unusable -- between "walk" and "stand still" the character is still
        decelerating, so the position never matched and got filtered out.
        """
        if self.addrs is None:
            print("  press F2 or F3 once first to seed the candidate set")
            beep(300, 200)
            return
        t = time.perf_counter()
        self._push_undo()
        now = read_values(self.p, self.addrs, self.dt)
        moved = np.zeros(len(self.addrs), dtype=bool)
        for _ in range(max(samples - 1, 1)):
            time.sleep(span / max(samples - 1, 1))
            nxt = read_values(self.p, self.addrs, self.dt)
            moved |= (raw(nxt) != raw(now))
            now = nxt
        self._keep(moved if must_move else ~moved, now)
        self._report_step("MOVING" if must_move else "STILL", t)
        beep(1200 if must_move else 700, 90)

    # -- bookkeeping shared by both filters --------------------------------

    def _push_undo(self):
        self.history.append((self.addrs, self.ref, self.last))
        if len(self.history) > 16:
            self.history.pop(0)

    def _keep(self, mask, now):
        self.addrs = self.addrs[mask]
        self.ref = self.ref[mask]
        self.last = now[mask]
        self.last_time = time.perf_counter()

    def _report_step(self, tag, t):
        self.steps += 1
        print(f"  step {self.steps:<3} [{tag:<7}] {len(self.addrs):>9} "
              f"candidates  ({time.perf_counter() - t:.2f}s)")

    def undo(self):
        if not self.history:
            beep(300, 200)
            return
        self.addrs, self.ref, self.last = self.history.pop()
        print(f"[F4] undo -> {len(self.addrs)} candidates")
        beep(500, 90)

    # -- timeline ---------------------------------------------------------

    def toggle_record(self):
        if self.addrs is None or len(self.addrs) == 0:
            print("  nothing to record; narrow the candidates first")
            beep(300, 200)
            return
        if len(self.addrs) > 400_000:
            print(f"  {len(self.addrs)} candidates is too many to sample at "
                  f"60 Hz; filter down below ~100k first")
            beep(300, 200)
            return
        self.recording = not self.recording
        if self.recording:
            self.rec_buf = []
            print(f"[F5] recording {len(self.addrs)} candidates... "
                  f"perform your moves, F5 again to stop")
            beep(1500, 60)
            beep(1800, 60)
        else:
            self.series = (np.stack(self.rec_buf)
                           if self.rec_buf else None)
            n = 0 if self.series is None else self.series.shape[0]
            print(f"[F5] stopped, {n} frames captured")
            beep(1800, 60)
            beep(1500, 60)

    def tick_record(self):
        if self.recording or self.active_label is not None:
            self.rec_buf.append(read_values(self.p, self.addrs, self.dt))

    # -- labelled sessions -------------------------------------------------

    def toggle_label(self, index):
        """Start/stop a recording session tagged with labels[index]."""
        if not self.labels:
            print("  start the scanner with --labels to use the numpad keys")
            beep(300, 200)
            return
        if index >= len(self.labels):
            return
        if self.addrs is None or len(self.addrs) == 0:
            print("  narrow the candidates with F2/F3 first")
            beep(300, 200)
            return
        name = self.labels[index]
        if self.active_label == name:
            self.sessions[name] = (np.stack(self.rec_buf)
                                   if self.rec_buf else None)
            n = 0 if self.sessions[name] is None else self.sessions[name].shape[0]
            print(f"  [{name}] stopped, {n} frames")
            self.active_label = None
            beep(700, 70)
            return
        if self.active_label is not None:
            self.toggle_label(self.labels.index(self.active_label))
        self.active_label = name
        self.rec_buf = []
        print(f"  [{name}] recording {len(self.addrs)} candidates... do ONLY "
              f"that attack type, NUM{index + 1} again to stop")
        beep(1600, 70)

    def report_labels(self):
        self.findings, used = analyse_labels(
            self.sessions, self.addrs, self.labels,
            max_exclusive=self.max_exclusive,
            max_switch=self.max_switch)
        missing = [l for l in self.labels if l not in used]
        if missing:
            print(f"  no usable session for: {', '.join(missing)}")
        if len(used) < 2:
            print("  record at least two labels before analysing")
            return
        print(f"\n=== {len(self.findings)} fields separate "
              f"{', '.join(used)} ===")
        if not self.findings:
            print("  nothing separates the labels. Record longer sessions, or\n"
                  "  loosen the candidate set (F4 to undo a filter or two).")
            return
        print(f"  {'address':<12}{'score':>8}{'switch':>8}   "
              f"exclusive values per label")
        for f in self.findings[:25]:
            vals = "  ".join(f"{l}={','.join(str(v) for v in vv)}"
                             for l, vv in f["values"].items())
            print(f"  0x{f['address']:012X}{f['score']:>8.3f}"
                  f"{f['switch_rate']:>8.3f}   {vals}")
        print()
        beep(1400, 120)

    def save_sessions(self, path="sessions.npz"):
        """Dump the raw recordings so the analysis can be re-tuned offline."""
        have = {l: s for l, s in self.sessions.items() if s is not None}
        if not have or self.addrs is None:
            return None
        blob = {"addrs": self.addrs, "dtype": np.array(self.name)}
        for l, s in have.items():
            blob[f"s_{l}"] = s
        np.savez_compressed(path, **blob)
        mb = sum(s.nbytes for s in have.values()) / 1048576
        print(f"  wrote {path} ({len(have)} sessions, {mb:.0f} MB raw)")
        return path

    # -- output -----------------------------------------------------------

    def report(self, top=25):
        if self.addrs is None:
            print("  no candidates yet")
            return
        print(f"\n=== {len(self.addrs)} candidates, dtype {self.name} ===")

        groups = cluster(self.addrs)
        groups.sort(key=lambda g: -g[2])
        print("  densest address clusters (a struct shows up as one cluster):")
        for lo, hi, n in groups[:8]:
            print(f"    0x{lo:012X}-0x{hi:012X}  span 0x{hi - lo:<6X} {n} hits")

        rows = []
        if self.series is not None and self.series.shape[0] > 4:
            score, distinct, switches = score_timeline(self.series)
            order = np.argsort(-score)[:top]
            print(f"\n  ranked by state-likeness over {self.series.shape[0]} "
                  f"recorded frames:")
            print(f"    {'address':<12}{'score':>7}{'distinct':>10}"
                  f"{'switches':>10}  values seen")
            for i in order:
                if score[i] <= 0:
                    continue
                vals = np.unique(self.series[:, i])
                shown = ", ".join(str(v) for v in vals[:8])
                if len(vals) > 8:
                    shown += ", ..."
                print(f"    0x{int(self.addrs[i]):012X}{score[i]:>7.3f}"
                      f"{distinct[i]:>10}{switches[i]:>10}  {shown}")
                rows.append({"address": int(self.addrs[i]),
                             "score": float(score[i]),
                             "distinct": int(distinct[i]),
                             "switches": int(switches[i]),
                             "values": [float(v) for v in vals[:32]]})
        else:
            print("  (record a timeline with F5 to rank these)")
            for a, v in zip(self.addrs[:top], self.ref[:top]):
                print(f"    0x{int(a):012X}  ref={v}")
        print()
        return rows

    def save(self, path="scan_result.json"):
        rows = self.report()
        if self.labels:
            self.report_labels()
            self.save_sessions()
        blob = {"dtype": self.name,
                "candidates": [int(a) for a in self.addrs[:5000]],
                "ranked": rows or [],
                "labels": self.labels,
                "label_findings": self.findings[:200]}
        with open(path, "w") as f:
            json.dump(blob, f, indent=2)
        print(f"[F7] wrote {path}")
        beep(1000, 120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default="DOA6LR.exe")
    ap.add_argument("--dtype", default="u32", choices=list(DTYPES))
    ap.add_argument("--max-exclusive", type=int, default=6,
                    help="most distinct values one label may own (default 6)")
    ap.add_argument("--max-switch", type=float, default=0.25,
                    help="reject fields that change on more than this fraction "
                         "of frames; animation data sits near 1.0 (default 0.25)")
    ap.add_argument("--labels", default="",
                    help="comma-separated labels; numpad 1-9 record a session "
                         "per label, F6 reports which field separates them")
    args = ap.parse_args()

    pid = find_pid(args.process)
    if pid is None:
        print(f"{args.process} is not running.")
        sys.exit(1)

    proc = Process(pid)
    print(__doc__)
    print(f"attached to {args.process} pid={pid}, scanning as {args.dtype}\n"
          f"tip: run the game in borderless/windowed mode so alt-tab is cheap\n")

    sc = Scanner(proc, DTYPES[args.dtype], args.dtype)
    sc.labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    sc.max_exclusive = args.max_exclusive
    sc.max_switch = args.max_switch
    if sc.labels:
        print("labelled sessions enabled:")
        for i, l in enumerate(sc.labels):
            print(f"  NUM{i + 1}  record {l!r}")
        print("  F6   analyse which field separates them")
        print()
    keys = Keys()
    actions = {"F1": sc.set_reference,
               "F2": lambda: sc.filter(True),
               "F3": lambda: sc.filter(False),
               "F8": lambda: sc.filter_activity(True),
               "F9": lambda: sc.filter_activity(False),
               "F11": lambda: sc.filter_delta(True),
               "F12": lambda: sc.filter_delta(False),
               "F4": sc.undo,
               "F5": sc.toggle_record,
               "F6": lambda: (sc.report(), sc.report_labels()
                              if sc.labels else None),
               "F7": sc.save}
    for _i in range(9):
        actions[f"NUM{_i + 1}"] = (lambda i=_i: sc.toggle_label(i))

    next_tick = time.perf_counter()
    try:
        while True:
            for k in keys.pressed():
                if k == "F10":
                    return
                actions[k]()
            sc.tick_record()
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


if __name__ == "__main__":
    main()
