"""
reanalyse.py - re-run the label analysis on sessions.npz without re-recording.

scanner.py writes sessions.npz on F7. Recording costs minutes of play; trying
a different threshold should cost seconds, so every knob is a flag here.

    python reanalyse.py                              # defaults
    python reanalyse.py --max-switch 0.05            # only very steady fields
    python reanalyse.py --max-exclusive 12           # moves with many sub-ids
    python reanalyse.py --width 2                    # read candidates as u16
    python reanalyse.py --describe 0x0DCA5B35        # what does one address do
"""

import argparse

import numpy as np

from scanner import analyse_labels


def load(path):
    z = np.load(path, allow_pickle=False)
    addrs = z["addrs"]
    labels = [k[2:] for k in z.files if k.startswith("s_")]
    sessions = {l: z[f"s_{l}"] for l in labels}
    return addrs, labels, sessions


def widen(sessions, addrs, width):
    """Reinterpret adjacent candidate columns as a wider integer.

    Only meaningful where candidates are contiguous in memory, which is exactly
    where a multi-byte field would hide.
    """
    if width == 1:
        return sessions, addrs
    keep = []
    for i in range(len(addrs) - width + 1):
        if all(int(addrs[i + k]) == int(addrs[i]) + k for k in range(width)):
            keep.append(i)
    if not keep:
        return sessions, addrs
    keep = np.array(keep)
    out = {}
    for lab, arr in sessions.items():
        acc = np.zeros((arr.shape[0], len(keep)), dtype=np.uint32)
        for k in range(width):
            acc |= arr[:, keep + k].astype(np.uint32) << (8 * k)
        out[lab] = acc
    return out, addrs[keep]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="sessions.npz")
    ap.add_argument("--max-exclusive", type=int, default=6)
    ap.add_argument("--max-switch", type=float, default=0.25)
    ap.add_argument("--width", type=int, default=1, choices=[1, 2, 4])
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--describe", default=None,
                    help="hex address: print its per-label value histogram")
    args = ap.parse_args()

    addrs, labels, sessions = load(args.file)
    order = ["high", "midp", "midk", "low"]
    labels = [l for l in order if l in labels] + \
             [l for l in labels if l not in order]
    print(f"{args.file}: {len(addrs)} candidates, labels {labels}")
    for l in labels:
        print(f"  {l:<6} {sessions[l].shape[0]:>6} frames")

    if args.describe:
        want = int(args.describe, 16)
        idx = np.flatnonzero(addrs == want)
        if not idx.size:
            print(f"0x{want:012X} is not in this candidate set")
            return
        c = int(idx[0])
        print(f"\n0x{want:012X} per-label value counts:")
        for l in labels:
            col = sessions[l][:, c]
            vals, cnt = np.unique(col, return_counts=True)
            top = sorted(zip(cnt, vals), reverse=True)[:12]
            rate = (col[1:] != col[:-1]).mean()
            print(f"  {l:<6} switch_rate={rate:.3f}  " +
                  "  ".join(f"{int(v)}x{int(n)}" for n, v in top))
        return

    sess, ad = widen(sessions, addrs, args.width)
    if args.width > 1:
        print(f"  reinterpreted as u{args.width * 8}: {len(ad)} contiguous slots")

    findings, used = analyse_labels(sess, ad, labels,
                                    max_exclusive=args.max_exclusive,
                                    max_switch=args.max_switch)
    print(f"\n=== {len(findings)} fields separate {', '.join(used)} "
          f"(max_switch={args.max_switch}, max_exclusive={args.max_exclusive}) ===")
    if not findings:
        print("  nothing. Raise --max-switch or --max-exclusive, or try --width 2")
        return
    print(f"  {'address':<12}{'score':>8}{'switch':>8}   exclusive values per label")
    for f in findings[:args.top]:
        vals = "  ".join(f"{l}={','.join(str(int(v)) for v in vv)}"
                         for l, vv in f["values"].items())
        print(f"  0x{f['address']:012X}{f['score']:>8.3f}"
              f"{f['switch_rate']:>8.3f}   {vals}")


if __name__ == "__main__":
    main()
