# Minato (char 35) combo verification

Two different things get called "working", and confusing them cost a whole
evening of reverts:

* **comboreplay** clears a Combo Challenge stage. It presses the inputs it just
  recorded, with the demo's own timing in hand, and closes the distance first.
  Every stage below has been cleared there.
* **holdbot `--test-combo`** presses the stored string with the match engine's
  input code. This is the one that predicts what happens in a fight, and it is
  a separate, younger implementation.

| sequence | comboreplay | `--test-combo` |
|---|---|---|
| `236P` | cleared | not tested |
| `214T,214T,214T,214T` | cleared | **4/4 at f0693be** |
| `HK,P,P,P,P` | cleared | **5/5 at bb86dac** (with +2 re-press) |
| `8K,S,6S,6S,6S` | cleared | 3rd input drops |
| `3K,3K,66P` | cleared | **3/3**, dash travel at the game's ceiling |
| `P,P,P,4,6P` | cleared | **5/5** (176>8045>8046>8118>8078) |
| `9K,6P,6P,6P` | cleared (wall only) | **4/4** (189>177>8065>8066) |
| `66P,8P,P,P,4K,K,K,K` | cleared | not tested |

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
  animation moves on rather than waiting a fixed time
* a recorded gap over 0.4 s means the demo waited for the previous move to
  END: pressing at half of it lands inside the animation and the game answers
  with the STRING continuation instead of the move wanted (9K's follow-up 6P
  came out as 8084 rather than the standing 177). Under 0.4 s it is a string
  branch and the halved timing plus re-presses is right.
* a step the recording skipped (a walk) still took time that belongs to the
  next gap

## Rule for changing any of this

Change one thing, then re-test **every** row that already says it passed. The
evening's reverts all came from fixing one sequence and silently breaking
another, because the fixes went into shared code paths.
