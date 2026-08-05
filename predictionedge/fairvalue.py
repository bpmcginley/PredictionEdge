"""Fair-value blending: de-vig consensus, optionally nudged by smart money."""

from __future__ import annotations

from .whales import WhaleSignal


def blended_fair_prob(
    devig_prob: float,
    whale: WhaleSignal | None = None,
    whale_weight: float = 0.0,
) -> float:
    """Blend the de-vig fair probability with a whale signal.

    The whale's pull is ``whale_weight * whale.confidence`` - so a low-confidence
    signal barely moves fair value even if whale_weight is high. With no signal,
    or zero weight, this is just the de-vig probability.
    """
    if whale is None or whale_weight <= 0.0:
        return _clip(devig_prob)
    w = max(0.0, min(1.0, whale_weight * whale.confidence))
    return _clip((1.0 - w) * devig_prob + w * whale.prob)


def _clip(p: float) -> float:
    return min(0.999, max(0.001, p))
