# Minato (char 35) combo verification

Two different things get called "working", and confusing them cost a whole
evening of reverts:

* **comboreplay** clears a Combo Challenge stage. It presses the inputs it just
  recorded, with the demo's own timing in hand, and closes the distance first.
  Every stage below has been cleared there.
* **holdbot `--test-combo`** presses the stored string with the match engine's
  input code. This is the one that predicts what happens in a fight, and it is
  a separate, younger implementation.

| sequence | `--test-combo` at `72d72af` |
|---|---|
| `214T,214T,214T,214T` | pass |
| `HK,P,P,P,P` | pass |
| `8K,S,6S,6S,6S` | pass |
| `3K,3K,66P` | pass |
| `P,P,P,4,6P` | pass |
| `9K,6P,6P,6P` | pass |
| `66P,8P,P,P,4K,K,K,K` | pass |
| `236P` | pass |

All eight confirmed against ONE build for the first time. Before this they had
been signed off at six different commits over 22 hours, each on a different
version of the same function, none re-checked after the code moved. Any change
from here re-runs all eight.

## The match engine does NOT use these timings

`--test-combo` and the match engine are separate code. The table above is the
harness. The engine, as of `72d72af`, still has the older rules:

| | harness (validated) | match engine |
|---|---|---|
| gap before a follow-up | recorded gap less 0.08 s, long gaps wait for the move to end | half the recorded gap, no long/short split |
| measured from | the instant the button went down | the loop tick before the press |
| re-press interval | 0.07 s from the end of the press | 0.04 s |
| re-press limit | 3 on a short gap, 12 on a long one | none |
| `combo_timing.json` | used | not read |

So a combo fires in a match, but not with the timing that was just verified.
The closing K of the Shuffle in particular has no pinned delay there, which is
the input that took six rounds to get right in the harness.

## What the input code has to get right

Each of these was measured, and each was found once in comboreplay and then
again, separately, in holdbot:

* a dash is two short taps and then direction+button TOGETHER. Measured with
  `--probe-dash 66P` against Minato:

  | input | result |
  |---|---|
  | forward held 0.13 / 0.20 / 0.30 / 0.45 s, then the button | 177 |
  | tap, 0.05 or 0.10 s, direction+button | 8077 |
  | tap, 0.20 s or more, direction+button | 177 |
  | tap, gap, second tap, direction+button | 8077 at 0.05, 0.10 and 0.20 s |
  | tap, 0.10 s, second tap HELD 0.15 s, button | 8077 |
  | ...held 0.25 s or longer | 177 |

  So the gap must be short and the button goes down with the direction, not
  after it. The 0.15 s hold is the ceiling and it is the whole travel: the demo
  itself only ran 0.155 s before attacking (distance 142 to 146).
* a down diagonal needs both directions down before the button (`3K` becomes
  `6K`)
* the button is held 45 ms; 20 ms is missed inside a string
* a direction inside a string needs ~50 ms of lead, not 17
* a throw chain's window is at the END of each part, so press until the
  animation moves on rather than waiting a fixed time. The recorded gap must
  NOT be slept in front of a part while `MoveKind` is 4, 5 or 6: that sleep
  misses the window and the rest of the chain comes out as throws from neutral
  (182 and 8138 instead of 8158 and 8160). This guard has now been lost to a
  revert twice. It is one condition, `if n_st and me.get("MoveKind") not in
  (4, 5, 6)`, and `214T,214T,214T,214T` is the row that catches it
* a recorded gap over 0.4 s means the demo waited for the previous move to
  END: pressing at half of it lands inside the animation and the game answers
  with the STRING continuation instead of the move wanted (9K's follow-up 6P
  came out as 8084 rather than the standing 177). Under 0.4 s it is a string
  branch and the halved timing plus re-presses is right.
* a step the recording skipped (a walk) still took time that belongs to the
  next gap
* a SHORT gap presses at a flat 0.04 s. `--probe-step 7` swept the closing K
  of the Shuffle from 0.04 s to 0.34 s in 0.02 s steps and exactly one delay
  produced the move: 0.04 s gave 8056, 0.06 s and everything after gave nothing
  at all. The press is not late at 0.06 s, it is ignored. At 0.04 s the move
  still arrived 0.232 s later against the demo's 0.234 s, so the input buffers:
  pressing early does not make the move early, it only makes sure the game
  takes it. "The gap less 0.08 s" was 0.154 s here, past the window; the other
  short gaps worked only because 0.044 s and 0.091 s are early by accident
* a LONG gap goes out at the recorded gap less 0.08 s, with up to three
  retries on a short gap and twelve on a long one, always measured from the
  END of the previous press. Timing the retries from the button instead fitted
  four presses inside the long gap of `66P,8P,P,P,4K,K,K,K`; the spares
  buffered and surfaced as the P string's third hit where the 4K belonged,
  8046 instead of 8053. The first press of a token times from the button, the
  retries after it do not.
* the two gap lengths want different retry spacing. A long gap keeps 0.07 s
  (0.115 s apart in practice) because that is where a spare press does damage.
  A short one gets 0.045 s (0.09 s apart): the closing K of the Shuffle had its
  first press eaten and its one retry landed after the stance had moved on, so
  it came out as a standing kick, 179 instead of 8056 The retries matter as much as the timing: every fraction of the gap
  tried (0.4, 0.5, gap-0.06) lost the closing K of the Shuffle the same way,
  one press eaten and a single retry landing after the stance had moved on.
  Twelve retries is too many - the spare presses buffer and eat the input after
  them, which turned a 4K into the P string's third hit.
* every interval is measured from the instant the BUTTON went down, not from
  when the press routine returned. It holds the button 0.045 s, so timing from
  the return counted that hold twice - once in the wait before the next token
  and once in every re-press - and put the second P of `HK,P,P,P,P` at 0.36 s
  where the demo has it at 0.218
* the window that watches for a move to appear is itself a floor on how fast
  the next input can go out; 0.12 s there held the 4K back whatever the timing
  rule said

## The match engine uses the same numbers

As of the commit that added this section, `holdbot.py` presses a combo with the
rules above rather than its own. It had been waiting half of every recorded gap
and re-pressing without a limit, so a string that passed `--test-combo` still
fell apart in a fight. The three places that now match:

* the gap gate: over 0.4 s, wait for our own `MoveKind` to return to 0, capped
  at three quarters of the gap; under it, the gap less 0.08 s
* the re-press interval: 0.07 s on a long gap, 0.045 s on a short one,
  always measured from the END of the press
* the re-press cap: twelve on a long gap, three on a short one. The cap sits in
  the give-up test, not in the condition, so a string that is never accepted
  still resets on its 1.2 s timeout

## Rule for changing any of this

Change one thing, then re-test **every** row that already says it passed. The
evening's reverts all came from fixing one sequence and silently breaking
another, because the fixes went into shared code paths.
