"""Robust timing fit between a forced subtitle and its anchors.

Model: ``reference_time = scale * forced_time + offset``. ``scale`` is restricted
to the framerate conversions that actually happen between releases, which keeps
the search exact and cheap: for each scale, the best offset is the densest
cluster of per-anchor offsets (a 1-D consensus, i.e. RANSAC without sampling).
"""

import statistics
from dataclasses import dataclass, field

# reference/forced ratios: identity, PAL speed-up/down, 24 <-> 23.976, 25 <-> 24.
SCALES = (
    1.0,
    25 / 23.976,
    23.976 / 25,
    24 / 23.976,
    23.976 / 24,
    25 / 24,
    24 / 25,
)

TOLERANCE = 0.6  # seconds an anchor may deviate from the fit and still count


@dataclass
class Fit:
    scale: float = 1.0
    offset: float = 0.0
    inliers: list = field(default_factory=list)  # one Anchor per forced cue
    residual_median: float = 0.0
    residual_p90: float = 0.0

    @property
    def inlier_count(self):
        return len(self.inliers)

    @property
    def is_identity(self):
        return self.scale == 1.0 and abs(self.offset) <= IDENTITY_OFFSET

    def apply(self, t):
        return self.scale * t + self.offset


IDENTITY_OFFSET = 0.25


def fit_anchors(anchors, scales=SCALES, tolerance=TOLERANCE):
    """Best fit over all anchors, or ``None`` if there are no anchors."""
    best = None
    for scale in scales:
        candidate = _fit_scale(anchors, scale, tolerance)
        if candidate is None:
            continue
        if best is None or _better(candidate, best):
            best = candidate
    return best


def _fit_scale(anchors, scale, tolerance):
    if not anchors:
        return None
    offsets = sorted(
        ((a.reference_time - scale * a.forced_time, a) for a in anchors), key=lambda x: x[0]
    )

    # Densest window of width 2*tolerance, counted in distinct forced cues.
    best_lo, best_hi, best_count = 0, 0, 0
    hi = 0
    for lo in range(len(offsets)):
        while hi < len(offsets) and offsets[hi][0] - offsets[lo][0] <= 2 * tolerance:
            hi += 1
        count = len({a.forced_index for _, a in offsets[lo:hi]})
        if count > best_count:
            best_lo, best_hi, best_count = lo, hi, count

    center = statistics.median(o for o, _ in offsets[best_lo:best_hi])
    inliers = _inliers(anchors, scale, center, tolerance)
    if not inliers:
        return None
    offset = statistics.median(a.reference_time - scale * a.forced_time for a in inliers)
    inliers = _inliers(anchors, scale, offset, tolerance)
    residuals = sorted(abs(a.reference_time - (scale * a.forced_time + offset)) for a in inliers)
    return Fit(
        scale=scale,
        offset=offset,
        inliers=inliers,
        residual_median=statistics.median(residuals),
        residual_p90=residuals[min(len(residuals) - 1, int(0.9 * len(residuals)))],
    )


def _inliers(anchors, scale, offset, tolerance):
    """Closest anchor per forced cue within tolerance."""
    best = {}
    for a in anchors:
        err = abs(a.reference_time - (scale * a.forced_time + offset))
        if err <= tolerance and (a.forced_index not in best or err < best[a.forced_index][0]):
            best[a.forced_index] = (err, a)
    return [a for _, a in sorted(best.values(), key=lambda x: x[1].forced_index)]


def _better(a, b):
    if a.inlier_count != b.inlier_count:
        return a.inlier_count > b.inlier_count
    # Same support: prefer no framerate change, then the tighter fit.
    if (a.scale == 1.0) != (b.scale == 1.0):
        return a.scale == 1.0
    return a.residual_median < b.residual_median
