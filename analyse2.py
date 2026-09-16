"""
analyse2.py - second pass over probe.npz once phase (+0x128) and the frame
counter (+0x174) are known: find a stored startup value, the opponent's
health drop, and print compact switch timelines for a few offsets.
"""

import sys

import numpy as np

PHASE, FRAME, MOVE = 0x128, 0x174, 0x68

z = np.load("probe.npz")
acts = sorted({k.split("__")[0] for k in z.files if "__" in k})
A = {a: z[f"{a}__A"] for a in acts}
B = {a: z[f"{a}__B"] for a in acts}
T = {a: z[f"{a}__t"] for a in acts}


def view(raw, w):
    dt = {1: np.uint8, 2: np.uint16, 4: np.uint32, 8: np.float32}[w]
    ww = 4 if w == 8 else w
    return raw[:, :(raw.shape[1] // ww) * ww].view(dt)


def switches(raw, off, w, t, label):
    v = view(raw, w)[:, off // w].astype(np.int64)
    ch = np.flatnonzero(v[1:] != v[:-1]) + 1
    s = f"{label:<9} +0x{off:<5X} {int(v[0])}"
    for i in ch:
        s += f" -[{t[i] * 60:.0f}]-> {int(v[i])}"
    print("  " + s)


moves = [a for a in ("punch", "kick", "lowkick", "fwdpunch") if a in acts]

print("=== P1 switch timelines (frame numbers in brackets) ===")
for a in moves:
    for off, w in ((MOVE, 2), (PHASE, 2), (0x578, 1), (0x1A4, 1), (0x5B0, 2),
                   (0x624, 1), (0x1BD8, 2), (0x630, 4), (0x160C, 2)):
        switches(A[a], off, w, T[a], a)
    print()

print("=== P2 (B) switch timelines ===")
for a in moves:
    for off, w in ((MOVE, 2), (PHASE, 2), (0x578, 1), (FRAME, 2), (0x630, 4)):
        switches(B[a], off, w, T[a], a)
    print()

# ---- stored startup? ------------------------------------------------------
print("=== startup: frame counter value when phase turned 1 ===")
start_val = {}
for a in moves:
    ph = view(A[a], 2)[:, PHASE // 2]
    fr = view(A[a], 2)[:, FRAME // 2]
    i = np.flatnonzero(ph == 1)
    if not len(i):
        print(f"  {a}: never active"); continue
    i0 = i[0]
    i2 = np.flatnonzero(ph == 2)
    act_len = (i2[0] - i0) if len(i2) else None
    start_val[a] = (int(fr[i0]), i0, act_len)
    print(f"  {a:<9} active at counter={fr[i0]} (frame idx {i0}), "
          f"active frames ~{act_len}, prev counter {fr[i0 - 1]}")

print("\n=== fields constant during each move whose value == startup (±1) ===")
for w in (1, 2, 4):
    hits = None
    for a, (sv, i0, _) in start_val.items():
        V = view(A[a], w)
        mv = view(A[a], 2)[:, MOVE // 2]
        win = np.flatnonzero(mv != 0)                 # frames inside the move
        seg = V[win].astype(np.int64)
        const = (seg == seg[0]).all(axis=0)
        ok = const & (np.abs(seg[0] - sv) <= 1)
        hits = ok if hits is None else (hits & ok)
    cols = np.flatnonzero(hits)
    print(f"-- width {w}: {len(cols)}")
    for c in cols[:30]:
        vals = [int(view(A[a], w)[np.flatnonzero(view(A[a], 2)[:, MOVE // 2] != 0)[0], c])
                for a in start_val]
        print(f"  +0x{c * w:<6X} " + " ".join(f"{a}={v}" for a, v in zip(start_val, vals)))

# ---- health: something in B drops once when the kick lands ---------------
print("\n=== P2 health candidates: drops exactly once at the hit frame during kick, else constant ===")
mvB = view(B["kick"], 2)[:, MOVE // 2]
hit = np.flatnonzero(mvB != 0)
if len(hit):
    h0 = hit[0]
    print(f"  P2 reaction starts at frame idx {h0} ({T['kick'][h0] * 60:.0f})")
    for w, name in ((2, "u16"), (4, "u32"), (8, "f32")):
        V = view(B["kick"], w)
        if w == 8:
            X = V.astype(np.float64)
            X[~np.isfinite(X)] = 0
        else:
            X = V.astype(np.int64)
        pre = X[:h0]
        post = X[h0 + 1:]
        cpre = (pre == pre[0]).all(axis=0)
        cpost = (post == post[0]).all(axis=0) if len(post) > 1 else np.ones(X.shape[1], bool)
        drop = pre[0] - post[0]
        ok = cpre & cpost & (drop > 0) & (drop < pre[0])
        if w == 8:
            ok &= (pre[0] > 1) & (pre[0] < 100000) & (drop > 0.5)
        else:
            ok &= (pre[0] > 5) & (pre[0] < 1000000)
        cols = np.flatnonzero(ok)
        print(f"-- {name}: {len(cols)}")
        for c in cols[:25]:
            print(f"  +0x{c * (4 if w == 8 else w):<6X} {pre[0, c]} -> {post[0, c]}  (-{drop[c]})")
    # same for the other hits
    for a in ("lowkick", "fwdpunch"):
        mv = view(B[a], 2)[:, MOVE // 2]
        hh = np.flatnonzero(mv != 0)
        print(f"  {a}: P2 reaction {'at idx %d' % hh[0] if len(hh) else 'none'}")
