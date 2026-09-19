# doa6lr_ai — auto-hold / anti-CPU bot for DOA6 Last Round

English | [繁體中文](README.zh-TW.md)

An auto-hold bot for **Dead or Alive 6 Last Round** (`DOA6LR.exe`, 64-bit, Windows).
It reads the opponent's move state straight out of game memory (move id, phase, frame,
strike type, throw command), sends the matching hold inside the startup window,
answers throws with a low kick / crouch / backdash / sidestep / throw break, and punishes
with combos it has learned. **Offline vs CPU only** (Versus, Training, Survival, Arcade).

> Running this against another person online is cheating. Don't.

---

## Results

![DOA6LR True Fighter high-score board: the top seven entries are all the bot's, playing Mai Shiranui and Nyotengu](docs/true_fighter_record.png)

The in-game **True Fighter** (survival) leaderboard after a week of runs. The top seven scores are
the bot's: 1st and 3rd as Mai Shiranui (5,977,400 / 3,822,600), 2nd and 4th–7th as Nyotengu
(5,090,800 down to 886,200). Places 8–10 are the untouched defaults.

Survival streaks per character (rounds won–lost in one sitting): Nyotengu 155–2 and 101–0,
Mai Shiranui 83–1 and 62–1, Kula Diamond 83–1. Hold rate 85–92% across a whole run,
100% is common in single matches.

**On Legend, the game's hardest CPU, the bot wins about 95% of rounds.** Recent sittings as
Minato: 81–1, 68–1 and 42–1.

## Status

- **Four characters trained: Nyotengu (id 21), Mai Shiranui (id 30), Kula Diamond (id 31),
  Minato (id 35).**
  Each has a poke, a combo pool and match statistics. Any other character falls back to a
  generic pool and a P poke and has to accumulate its statistics from scratch. On the opponent
  side the tables (`startup.json` etc.) cover only the CPU characters actually met so far; a new
  opponent has to be learned move by move (unknown moves are guarded by default, and each new
  offensive hold costs one grab before it is sidestepped).
- Startup frames of every opponent move, throw types, offensive holds (OH), unholdable moves,
  the best answer to each throw and the input that breaks it are **learned during play and
  saved as JSON**, then reused on the next start.
- Combos are compared by net damage (dealt minus taken) with a bandit, per own character.
  Juggle follow-ups are re-pressed until the launcher's recovery ends.
- Holds are attempted in hit stun too: the game buffers the input to the end of the stun
  (24001: 52/77). Stuns that turn out to be true combos or knockdowns are retired per id.
- Throws: neutral throws (CommandCode 363 / 400) break with T, 95/98 and 65/66. Command throws
  do not break (T / 6T / 4T / 2T ~1 in 40). Fast throws (≤ 8 frames to the grab) are answered with
  **2K**: a low attack has crouching status from its first frame and a standing throw whiffs on
  it (the crouch walk was 1/14 against the same throw). Against grapplers whose fast throw has no
  answer, the bot stops poking inside its reach, backs off, and grabs an idle opponent first with
  its own T (18/25 as Mai against char 2).
- Facing has no memory field. It is probed from the walk animation id, confirmed inside every
  hold, and flipped when the world vector between the fighters reverses across a throw or
  knockdown.
- Opponent changes (Survival), sitting on P1 or P2, 3-way vs 4-way hold setting and deliberately
  added input lag are detected automatically or covered by a flag.

## Requirements

- Windows 10/11, DOA6 Last Round (Steam)
- Python 3.10+. `holdbot.py` uses the standard library only (`ctypes` for memory reads,
  `SendInput` for the keyboard)
- The scanning tools (`autoscan.py`, `probe.py`, `timeline.py`, `valuescan.py`) also need `numpy`
- Optional: `pip install vgamepad` (virtual Xbox pad, needs ViGEmBus)
- Run the terminal **as Administrator** (`ReadProcessMemory` needs it)

In-game settings: default keyboard binds (H = J, P = K, K = L, T = M; U = P+K, I = S, O = H+K).
The hold setting in Training must match `--hold-mode`.

## Quick start

```powershell
git clone https://github.com/yuchinchenTW/doa6lr_ai.git
cd doa6lr_ai
python fields.py                      # check the address table still resolves (prints both health values)
python holdbot.py --dry-run           # decide and print only, no input
python holdbot.py --hold-mode 4way    # play (game set to 4-way holds)
python holdbot.py                     # default 3-way
```

Start a match (or Training) first. The bot finds the position rows, works out whether the keyboard
drives P1 or P2, then starts. `Ctrl-C` stops it and prints the statistics.

## Common flags

| Flag | Default | Meaning |
|---|---|---|
| `--hold-mode 3way\|4way` | `3way` | Must match the game setting; in 3-way mid kicks use 4H too |
| `--me auto\|P1\|P2` | `auto` | Detect which side the keyboard controls |
| `--window N` | 16 | Hold when this many or fewer frames remain before the strike goes active |
| `--poke ...` | `auto` | Poke while the opponent idles, chosen per character (Nyotengu P+K, Mai 4P, Kula 6P) |
| `--combo ...` | `auto` | String after a hit; `auto` compares the character's combo pool |
| `--no-hold-in-stun` | off | By default holds are attempted in hit stun too |
| `--no-break-blow` | off | By default a full Break Gauge is spent on 6S Break Blow as the punish |
| `--break-hold` | off | Enable 4S Break Hold |
| `--throw-answer crouch\|jab\|none` | `crouch` | Base answer to throws; the best answer per throw is learned on top (fast throws start on 2K) |
| `--close-throw-range N` | 75 | Inside a fast unanswerable throw's reach, grab an idle opponent this close with our own T (0 disables) |
| `--unknown guard\|skip\|hold` | `guard` | What to do with a move whose startup is not learned yet |
| `--dry-run` / `--probe` | | No input / detection and startup learning only |

Full list: `python holdbot.py --help`.

## What it learns and where

| File | Key | Content |
|---|---|---|
| `startup.json` | opponent character + move | Frame the move goes active (the frame Phase turns 0→1) |
| `throws.json` | opponent character + move | CommandCode, high/mid/low and the distance each throw starts from |
| `throw_answers.json` | opponent character + move | Successes of low kick / crouch / back / side / guard; switches after two failures |
| `throw_escapes.json` | throw CommandCode (pooled across characters) | Successes of each break input (T / 6T / 4T / 2T) |
| `close_throw.json` | own character : opponent character | Our own neutral T on an idle opponent inside 75 units: grabbed / tried; dropped under 30% |
| `oh.json` | opponent character | Offensive holds (moves that grab a hold) |
| `nohold.json` | opponent character | Moves a hold "caught" for 0 damage; guarded instead |
| `stun_holds.json` | our hit-reaction animation id | Holds tried in that stun; retired at 0/3, under 15% after 10, or when it turns out to be a knockdown |
| `combo_stats.json` | own character + opener | Tries and net damage of every string |
| `commands.json` | our input | CommandCode and move id each keyboard input produces (`comboreplay.py --calibrate`), plus codes learned by replaying |
| `pos.json` | | Offset of the position field inside the character object |
| `layout.json` | | Memory layout (static pointer chains + field offsets) |

Switching your own character does not touch the opponent tables; they are keyed by opponent id.

## Reading the output

`#n` hold, `~` throw answer (`ours:` is our move id sequence), `*` poke, `+` combo, `=` guard,
`>` attack on a run-up, `<` wake-up backdash, `^` rising from the ground, `T` throw first,
`v` crouching under a fast throw at the wall, `!` something learned or a grab, `(skip)` seen but
unable to act, `THROWN` the last 20 state changes before a throw.
The summary lists hit rates per category, damage taken by opponent move and by what we had just
done, the combo ranking, the throw-break table and the round tally.

## Combo Challenge (experimental)

`comboreplay.py` watches the game's own demonstration in Combo Challenge - every input the demo
makes shows up as a CommandCode change, with the move it produced and the interval since the
previous input - and plays the same inputs back (F8), closing in to the demo's distance before each
task. Unknown codes are tried against a candidate list and remembered in `commands.json` once
they produce the demo's move. `--calibrate` presses every keyboard input once and prints the
code and move each produces; `--probe-cmd N` hunts for the input recipe behind one code (this is
how "← → P+K" was identified as cmd 5780). It clears single-move stages and most of a multi-task
stage (11 of 19 inputs on one); tasks that need a specific move variant at range are still open.

## Layout

| File | Purpose |
|---|---|
| `holdbot.py` | Main loop: detect → learn → facing probe → hold / guard / throw answer / combo |
| `comboreplay.py` | Combo Challenge: record the demonstration from memory, replay it, calibrate inputs |
| `valuescan.py` | Exact-value memory scan with interactive refinement (for numbers you can read on screen) |
| `fields.py` + `layout.json` | Pointer chain resolution and field reads |
| `pad.py` | Keyboard / virtual pad injection, hold table |
| `memlib.py` | `ReadProcessMemory` wrapper, region enumeration |
| `autoscan.py`, `probe.py`, `timeline.py`, `analyse2.py`, `pointerscan.py`, `scanner.py`, `watch.py`, `reanalyse.py` | Field-hunting toolchain; rerun when a game update breaks `layout.json` |
| `docs/NOTES.md` | Research log (Traditional Chinese): how the addresses were found, measured field values, DOA6 input pitfalls, match results per version |

## Known limits

- Inputs go through keyboard `SendInput`. Directions register when sent in the right order
  (horizontal a frame ahead, vertical together with the button); a diagonal or a motion (236P)
  comes out only if the character has that move, otherwise the game picks the nearest one.
  Up or down alone is a free step, so a vertical poke (8P) is unreliable in a match.
- A 7-frame dash throw started inside 100 units leaves no reaction time except 2K; a grappler's
  standing-throw / low-throw mixup stays a coin flip.
- Command throws cannot be broken; every new opponent costs one grab per offensive hold.
- There is no facing field in memory; a hold with very few frames left can still come out
  mirrored after a side swap.
- The address table matches the Last Round build released 2026-06; after a game update the
  toolchain has to be rerun.

## License

MIT, see `LICENSE`.
