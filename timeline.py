"""
timeline.py - per-frame view of chosen offsets in probe.npz, plus a search
for phase-like fields (idle-constant, a few piecewise-constant runs per move).

    python timeline.py --obj A --acts punch,kick,lowkick \
        --pick 0x593:1,0x794:1,0x904:1,0x68:2,0x100:2,0x174:2,0x1608:4
"""

import argparse

import numpy as np


def load(path, obj):
    z = np.load(path)
    acts = sorted({k.split("__")[0] for k in z.files if "__" in k})
    raw = {a: z[f"{a}__{obj}"] for a in acts}
    t = {a: z[f"{a}__t"] for a in acts}
    return acts, raw, t


def view(raw, w):
    dt = {1: np.uint8, 2: np.uint16, 4: np.uint32}[w]
    return raw[:, :(raw.shape[1] // w) * w].view(dt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="probe.npz")
    ap.add_argument("--obj", default="A")
    ap.add_argument("--acts", default="punch,kick,lowkick")
    ap.add_argument("--pick", default="")
    ap.add_argument("--phase", action="store_true",
                    help="search for phase-like fields during the first act")
    ap.add_argument("--min-run", type=int, default=3)
    args = ap.parse_args()

    acts, raw, t = load(args.file, args.obj)
    views = {a: {w: view(raw[a], w) for w in (1, 2, 4)} for a in acts}
    picks = []
    for part in args.pick.split(","):
        if ":" in part:
            o, w = part.split(":")
            picks.append((int(o, 0), int(w)))

    if picks:
        for act in args.acts.split(","):
            tt = t[act]
            print(f"\n=== {act} ({args.obj}) ===")
            print("   t    fr | " + " ".join(f"{('+%X' % o):>8}" for o, _ in picks))
            last = None
            for i in range(len(tt)):
                row = tuple(int(views[act][w][i, o // w]) for o, w in picks)
                if row != last or i == len(tt) - 1:
                    print(f"{tt[i]:5.2f} {tt[i] * 60:5.1f} | "
                          + " ".join(f"{v:>8}" for v in row))
                    last = row

    if args.phase:
        act = args.acts.split(",")[0]
        tp = t[act]
        print(f"\n=== phase-like fields during {act}: idle-constant, 1..4 "
              f"switches, runs >= {args.min_run} frames ===")
        for w in (1, 2, 4):
            idle = views["idle"][w]
            ic = (idle == idle[0]).all(axis=0)
            P = views[act][w]
            ch = (P[1:] != P[:-1])
            nsw = ch.sum(axis=0)
            cand = np.flatnonzero(ic & (nsw >= 1) & (nsw <= 4))
            rows = []
            for c in cand:
                idx = np.flatnonzero(ch[:, c]) + 1
                runs = np.diff(np.concatenate(([0], idx, [len(P)])))
                if runs.min() < args.min_run:
                    continue
                vals = [int(P[0, c])] + [int(P[i, c]) for i in idx]
                if w == 1 and max(vals) > 250:
                    continue
                rows.append((c * w, [round(float(tp[i]) * 60, 1) for i in idx],
                             vals, int(idle[0, c])))
            print(f"-- width {w}: {len(rows)}")
            for off, when, vals, iv in rows[:80]:
                print(f"  +0x{off:<7X} idle={iv:<6} switch@frames {when}  "
                      f"values {vals}")


if __name__ == "__main__":
    main()
