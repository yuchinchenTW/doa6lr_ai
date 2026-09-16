# doa6lr_ai — auto-hold / anti-CPU bot for DOA6 Last Round

[繁體中文](README.md) | English

An auto-hold bot for **Dead or Alive 6 Last Round** (`DOA6LR.exe`, 64-bit, Windows).
It reads the opponent's move state straight out of game memory (move id, phase, frame,
strike type, throw command), sends the matching hold inside the startup window,
answers throws with crouch / backdash / sidestep / throw escape, and punishes with
combos it has learned. **Offline vs CPU only** (Versus, Training, Survival, Arcade).

> Running this against another person online is cheating. Don't.

---

## Status

- 90–100% hold rate at normal input latency; Survival runs of 155–2 and 62–1 against the CPU.
- Startup frames of every opponent move, throw types, offensive holds (OH), unholdable moves and
  the best answer to each throw are **learned during play and saved as JSON**, then reused on
  the next start.
- Combos are compared by net damage (dealt minus taken) with a bandit, per own character.
- Opponent changes (Survival), sitting on P1 or P2, 3-way vs 4-way hold setting and deliberately
  added input lag are detected automatically or covered by a flag.
- **Trained only with Nyotengu (id 21) and Mai Shiranui (id 30).** Offence tables (poke, combo
  pool) and combo statistics exist for these two only. Any other character falls back to a generic
  combo pool and a P poke and has to accumulate its statistics from scratch. On the opponent side
  the tables (`startup.json` etc.) cover only the CPU characters actually met so far; a new
  opponent has to be learned move by move (unknown moves are guarded by default).

## Requirements

- Windows 10/11, DOA6 Last Round (Steam)
- Python 3.10+. `holdbot.py` uses the standard library only (`ctypes` for memory reads,
  `SendInput` for the keyboard)
- The scanning tools (`autoscan.py`, `probe.py`, `timeline.py`) also need `numpy`
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
| `--poke ...` | `auto` | Poke while the opponent idles, chosen per character (Nyotengu P+K, Mai 4P) |
| `--combo ...` | `auto` | String after a hit; `auto` compares the character's combo pool |
| `--no-hold-in-stun` | off | By default holds are attempted in hit stun too (the game buffers the input to the end of the stun) |
| `--no-break-blow` | off | By default a full Break Gauge is spent on 6S Break Blow as the punish |
| `--break-hold` | off | Enable 4S Break Hold |
| `--throw-answer crouch\|jab\|none` | `crouch` | Base answer to throws; the best answer per throw is learned on top |
| `--unknown guard\|skip\|hold` | `guard` | What to do with a move whose startup is not learned yet |
| `--dry-run` / `--probe` | | No input / detection and startup learning only |

Full list: `python holdbot.py --help`.

## What it learns and where

| File | Key | Content |
|---|---|---|
| `startup.json` | opponent character + move | Frame the move goes active (the frame Phase turns 0→1) |
| `throws.json` | opponent character + move | CommandCode and high/mid/low of each throw |
| `throw_answers.json` | opponent character + move | Successes of crouch / back / side / guard; switches after two failures |
| `throw_escapes.json` | opponent character + throw | Successes of each break input (T / 6T / 4T / 2T); guessed from the CommandCode first, switched after two failures |
| `oh.json` | opponent character | Offensive holds (moves that grab a hold) |
| `nohold.json` | opponent character | Moves a hold "caught" for 0 damage; guarded instead |
| `stun_holds.json` | our hit-reaction animation id | Holds tried in that stun; retired at 0/3 or when it turns out to be a knockdown |
| `combo_stats.json` | own character + opener | Tries and net damage of every string |
| `pos.json` | | Offset of the position field inside the character object |
| `layout.json` | | Memory layout (static pointer chains + field offsets) |

Switching your own character does not touch the opponent tables; they are keyed by opponent id.

## Reading the output

`#n` hold, `~` throw answer (`ours:` is our move id sequence), `*` poke, `+` combo, `=` guard,
`>` attack on a run-up, `<` wake-up backdash, `^` rising from the ground, `!` something learned or
a grab, `(skip)` seen but unable to act, `THROWN` the last 20 state changes before a throw.
The summary lists hit rates per category, damage taken by opponent move and by what we had just
done, the combo ranking and the round tally.

## Layout

| File | Purpose |
|---|---|
| `holdbot.py` | Main loop: detect → learn → facing probe → hold / guard / throw answer / combo |
| `fields.py` + `layout.json` | Pointer chain resolution and field reads |
| `pad.py` | Keyboard / virtual pad injection, hold table |
| `memlib.py` | `ReadProcessMemory` wrapper, region enumeration |
| `autoscan.py`, `probe.py`, `timeline.py`, `analyse2.py`, `pointerscan.py`, `scanner.py`, `watch.py`, `reanalyse.py` | Field-hunting toolchain; rerun when a game update breaks `layout.json` |
| `docs/NOTES.md` | Research log (Traditional Chinese): how the addresses were found, measured field values, DOA6 input pitfalls, match results per version |

## Known limits

- Keyboard `SendInput` cannot produce diagonal + button inputs (3P, 1P, ...), so combo pools use
  buttons and horizontals only.
- A 7-frame dash throw started inside 100 units leaves no reaction time; only a pre-emptive poke helps.
- There is no facing field in memory; facing is probed from the walk animation id. The first hold
  after a side swap can come out mirrored when very few frames remain.
- The address table matches the Last Round build released 2026-06; after a game update the
  toolchain has to be rerun.

## License

MIT, see `LICENSE`.
