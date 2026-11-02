"""Gradual-transition (dissolve / fade) and flash detection on frame thumbnails.

Model: inside a dissolve from shot A to shot B, every frame is approximately a
linear blend  F_k ≈ (1 - a_k) * A + a_k * B  with a_k rising from 0 to 1.
A fade is the same blend with A or B black. Pans, zooms and moving characters
are *not* blends of the window's endpoints, so they leave a large residual and
are rejected. That blend test is the discriminator; frame-difference
thresholds alone cannot separate a dissolve from motion.

Thumbnails come from one ffmpeg decode (default 64x36 RGB, values 0..1). All
frame indices are decoded-frame indices, matching breakdown.py.

The per-frame blend weights a_k are returned, so a remake can reproduce the
source's exact dissolve curve when compositing redesigned shots.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Params:
    widths: tuple = (4, 6, 8, 12, 16, 24, 36)   # half-widths tried, in frames
    min_change: float = 0.06       # mean |A-B| (0..1) across the window to count as a transition
    mid_residual: float = 0.35     # prefilter: |F_mid - (A+B)/2| / |A-B|
    max_residual: float = 0.30     # median relative blend residual inside the window
    min_monotonic: float = 0.90    # Spearman correlation of a_k with time
    max_alpha_jitter: float = 0.20  # max deviation of a_k from the fitted 0→1 ramp
    motion_ratio: float = 0.6      # blend error must be below this × best shift/zoom error
    same_structure: float = 0.6    # edge correlation of the endpoints above this = same drawing
    min_length: int = 4            # frames; shorter blends are treated as cuts
    flash_max_frames: int = 3
    flash_same_content: float = 0.05   # |before - after| for a flash that returns to the same shot
    flash_brightness: float = 0.12     # mean luma rise of the flash frames over their neighbours
    dark_level: float = 0.06           # mean luma below which an endpoint counts as black


@dataclass
class Transition:
    start: int                 # first frame of the transition
    end_excl: int              # first frame after it
    kind: str                  # dissolve | fade_in | fade_out
    alpha: list = field(default_factory=list)   # blend weight per frame in [start, end_excl)
    residual: float = 0.0      # median relative blend residual
    monotonic: float = 0.0
    change: float = 0.0

    def as_dict(self):
        return {"start_frame": self.start, "end_frame_excl": self.end_excl, "frames": self.end_excl - self.start,
                "kind": self.kind, "alpha": [round(a, 4) for a in self.alpha],
                "blend_residual": round(self.residual, 4), "monotonic": round(self.monotonic, 4),
                "change": round(self.change, 4)}


def thumbnails(path: str, w: int = 64, h: int = 36, crop: str | None = None) -> np.ndarray:
    """Decode every frame of the first video stream to a (N, h, w, 3) uint8 array."""
    vf = (f"crop={crop}," if crop else "") + f"scale={w}:{h}:flags=area,format=rgb24"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path, "-map", "0:v:0",
                        "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    n = len(r.stdout) // (w * h * 3)
    return np.frombuffer(r.stdout[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)


def _spearman(x: np.ndarray) -> float:
    if len(x) < 3 or np.ptp(x) == 0:
        return 0.0
    rx = np.argsort(np.argsort(x, kind="stable"), kind="stable").astype(float)
    ry = np.arange(len(x), dtype=float)
    rx -= rx.mean(); ry -= ry.mean()
    return float((rx * ry).sum() / np.sqrt((rx ** 2).sum() * (ry ** 2).sum()))


def _fit(F: np.ndarray, a: int, b: int):
    """Blend fit of frames a..b (inclusive) against endpoints F[a], F[b]."""
    A, B = F[a], F[b]
    D = B - A
    dd = float((D * D).sum())
    seg = F[a:b + 1] - A
    alpha = np.clip((seg * D).sum(axis=1) / dd, 0.0, 1.0)
    resid = np.sqrt(((seg - alpha[:, None] * D) ** 2).sum(axis=1)) / np.sqrt(dd)
    return alpha, resid


def _ramp_fit(alpha: np.ndarray):
    """Least-squares fit of a 0 → linear ramp → 1 profile to alpha.

    Returns (s, e) as indices into alpha: alpha is ~0 before s, ramps over
    [s, e), and is ~1 from e on. Robust to the per-frame noise that motion
    inside a dissolve adds, unlike thresholding alpha directly.
    """
    a = np.asarray(alpha, np.float64)
    n = len(a)
    k = np.arange(n, dtype=np.float64)
    z = np.zeros(1)
    S1 = np.concatenate([z, np.cumsum(a)])            # Σ a
    S2 = np.concatenate([z, np.cumsum(a * a)])        # Σ a²
    SK = np.concatenate([z, np.cumsum(k * a)])        # Σ k·a
    C1 = np.concatenate([z, np.cumsum((1 - a) ** 2)])  # cost of predicting 1
    s = np.arange(n)[:, None]
    e = np.arange(n + 1)[None, :]
    L = (e - s).astype(np.float64)
    valid = L >= 2
    Ls = np.where(valid, L, 1.0)
    # Σ_{k=s}^{e-1} (a_k - (k-s+1)/(L+1))², expanded with prefix sums
    sum_ja = (SK[e] - SK[s]) - (s - 1) * (S1[e] - S1[s])
    ramp_cost = (S2[e] - S2[s]) - 2 * sum_ja / (Ls + 1) + Ls * (Ls + 1) * (2 * Ls + 1) / 6 / (Ls + 1) ** 2
    cost = S2[s] + ramp_cost + (C1[n] - C1[e])
    cost = np.where(valid, cost, np.inf)
    i, j = np.unravel_index(int(np.argmin(cost)), cost.shape)
    return int(i), int(j)


def _edge_corr(A: np.ndarray, B: np.ndarray) -> float:
    """Pearson correlation of gradient magnitudes: ~1 when both frames show
    the same drawing (whatever its colours), low for different pictures."""
    def gm(x):
        gx = np.diff(x, axis=1)[:-1, :]
        gy = np.diff(x, axis=0)[:, :-1]
        return np.sqrt(gx * gx + gy * gy).ravel()
    a, b = gm(A), gm(B)
    a = a - a.mean(); b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 1e-9 else 0.0


def _gray(x: np.ndarray, h: int, w: int) -> np.ndarray:
    return (x.reshape(h, w, 3) @ np.array([0.299, 0.587, 0.114], np.float32))


def _scale(img: np.ndarray, s: float) -> np.ndarray:
    """Zoom about the centre by factor s (bilinear, same size)."""
    if s == 1.0:
        return img
    h, w = img.shape
    yy = (np.arange(h) - (h - 1) / 2) / s + (h - 1) / 2
    xx = (np.arange(w) - (w - 1) / 2) / s + (w - 1) / 2
    y0 = np.clip(np.floor(yy).astype(int), 0, h - 2); x0 = np.clip(np.floor(xx).astype(int), 0, w - 2)
    fy = np.clip(yy - y0, 0, 1)[:, None]; fx = np.clip(xx - x0, 0, 1)[None, :]
    a = img[y0][:, x0]; b = img[y0][:, x0 + 1]; c = img[y0 + 1][:, x0]; d = img[y0 + 1][:, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def _motion_error(M: np.ndarray, R: np.ndarray, max_dx: int, max_dy: int,
                  scales=(1.0, 0.9, 1.1, 0.95, 1.05, 0.85, 1.18), stop_below: float = -1.0) -> float:
    """Smallest mean |M - warp(R)| over integer shifts and centre zooms,
    measured on the central region every shift leaves valid. Returns as soon
    as an error below `stop_below` is found (motion already wins)."""
    h, w = M.shape
    core = M[max_dy:h - max_dy, max_dx:w - max_dx]
    best = np.inf
    for s in scales:
        if best < stop_below:
            break
        Rs = _scale(R, s)
        for dy in range(-max_dy, max_dy + 1):
            for dx in range(-max_dx, max_dx + 1):
                cand = Rs[max_dy + dy:h - max_dy + dy, max_dx + dx:w - max_dx + dx]
                err = float(np.abs(core - cand).mean())
                if err < best:
                    best = err
    return best


def detect_gradual(thumbs: np.ndarray, hard_cuts, p: Params = Params()) -> list[Transition]:
    """Find dissolves and fades. `hard_cuts` are frame indices where a new shot starts."""
    n = len(thumbs)
    F = thumbs.reshape(n, -1).astype(np.float32) / 255.0
    luma = F.reshape(n, -1, 3) @ np.array([0.299, 0.587, 0.114], np.float32)
    mean_luma = luma.mean(axis=1)
    cut = np.zeros(n + 1, bool)
    for c in hard_cuts:
        if 0 < c < n:
            cut[c] = True
    cut_cum = np.cumsum(cut)

    h, w_px = thumbs.shape[1], thumbs.shape[2]
    max_dx, max_dy = max(2, w_px // 8), max(2, h // 7)
    motion_cache: dict = {}

    def blend_beats_motion(s: int, e: int) -> tuple[bool, float, float]:
        """Is the middle frame better explained as a blend of the pure frames
        just outside [s, e) than as a shifted/zoomed copy of either one?"""
        key = (s, e)
        if key not in motion_cache:
            a2, b2, m = s - 1, e, (s + e) // 2
            A, B, M = (_gray(F[i], h, w_px) for i in (a2, b2, m))
            D = B - A
            al = float(np.clip(((M - A) * D).sum() / max(float((D * D).sum()), 1e-9), 0, 1))
            r_blend = float(np.abs(M - (A + al * D)).mean())
            stop = r_blend / p.motion_ratio
            r_motion = _motion_error(M, A, max_dx, max_dy, stop_below=stop)
            if r_motion >= stop:
                r_motion = min(r_motion, _motion_error(M, B, max_dx, max_dy, stop_below=stop))
            motion_cache[key] = (r_blend <= p.motion_ratio * r_motion, r_blend, r_motion)
        return motion_cache[key]

    cands = []
    for w in p.widths:
        if 2 * w + 1 > n:
            continue
        t = np.arange(w, n - w)
        A, B, M = F[t - w], F[t + w], F[t]
        change = np.abs(B - A).mean(axis=1)
        mid = np.abs(M - 0.5 * (A + B)).mean(axis=1) / np.maximum(change, 1e-6)
        # no hard cut strictly inside (t-w, t+w]
        no_cut = (cut_cum[t + w] - cut_cum[t - w]) == 0
        for ti in t[(change >= p.min_change) & (mid <= p.mid_residual) & no_cut]:
            a, b = ti - w, ti + w
            alpha, resid = _fit(F, a, b)
            med = float(np.median(resid[1:-1]))
            mono = _spearman(alpha)
            if med > p.max_residual or mono < p.min_monotonic:
                continue
            rs, re_ = _ramp_fit(alpha)
            s, e = a + rs, a + re_
            if e - s < p.min_length or s < 1 or e > n - 1:
                continue
            # the blend weight must follow the fitted ramp closely
            ramp = (np.arange(e - s) + 1) / (e - s + 1)
            if float(np.abs(alpha[rs:re_] - ramp).max()) > p.max_alpha_jitter:
                continue
            s, e = int(s), int(e)
            cands.append((-(e - s), med, s, e, mono, float(change[ti - w])))

    # longest valid interval first, then best fit; drop overlaps. The costly
    # motion and structure tests run only on candidates that survive overlap.
    cands = sorted(set(cands))
    taken = np.zeros(n, bool)
    out: list[Transition] = []
    for _, med, s, e, mono, ch in cands:
        if taken[max(0, s - 1):min(n, e + 1)].any():
            continue
        if not blend_beats_motion(s, e)[0]:
            continue
        dark_a, dark_b = mean_luma[s - 1] < p.dark_level, mean_luma[e] < p.dark_level
        if not (dark_a or dark_b) and _edge_corr(_gray(F[s - 1], h, w_px), _gray(F[e], h, w_px)) > p.same_structure:
            continue   # same drawing before and after: an in-shot colour/light change, not a transition
        # blend weights for the stored curve, against the pure frames just outside
        alpha, _ = _fit(F, s - 1, e)
        alpha = alpha[1:-1]
        kind = "dissolve"
        if mean_luma[s - 1] < p.dark_level <= mean_luma[e]:
            kind = "fade_in"
        elif mean_luma[e] < p.dark_level <= mean_luma[s - 1]:
            kind = "fade_out"
        out.append(Transition(s, e, kind, alpha.tolist(), med, mono, ch))
        taken[s:e] = True
    return sorted(out, key=lambda x: x.start)


def detect_flashes(thumbs: np.ndarray, hard_cuts, p: Params = Params()):
    """Pairs of hard cuts at most `flash_max_frames` apart around a bright spike.

    Returns (flashes, cuts_to_drop). A flash that returns to the same picture
    is not a shot change, so both of its cuts are dropped. A flash that leads
    into a different picture keeps its cuts; the flash frames are labelled.
    """
    n = len(thumbs)
    F = thumbs.reshape(n, -1).astype(np.float32) / 255.0
    luma = (F.reshape(n, -1, 3) @ np.array([0.299, 0.587, 0.114], np.float32)).mean(axis=1)
    cuts = sorted(c for c in hard_cuts if 0 < c < n)
    flashes, drop = [], set()
    for c1, c2 in zip(cuts, cuts[1:]):
        if c2 - c1 > p.flash_max_frames or c1 - 1 < 0 or c2 >= n:
            continue
        rise = float(luma[c1:c2].mean() - max(luma[c1 - 1], luma[c2]))
        if rise < p.flash_brightness:
            continue
        same = float(np.abs(F[c1 - 1] - F[c2]).mean()) < p.flash_same_content
        flashes.append({"start_frame": c1, "end_frame_excl": c2, "frames": c2 - c1,
                        "luma_rise": round(rise, 4), "returns_to_same_shot": same})
        if same:
            drop.update((c1, c2))
    return flashes, drop
