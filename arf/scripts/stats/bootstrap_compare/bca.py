from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

import numpy as np
from scipy.stats import bootstrap

from arf.scripts.stats.bootstrap_compare.constants import (
    BOOTSTRAP_ITERATIONS,
    BOOTSTRAP_SEED,
    CONFIDENCE_LEVEL,
    Statistic,
)

type StatFn = Callable[[np.ndarray, int], np.ndarray]


@dataclass(frozen=True, slots=True)
class PairedDelta:
    statistic: Statistic
    delta: float
    lower: float
    upper: float
    confidence_level: float
    n_pairs: int
    n_resamples: int


def _stat_mean(x: np.ndarray, axis: int = -1) -> np.ndarray:
    return np.asarray(np.mean(x, axis=axis))


def _stat_p50(x: np.ndarray, axis: int = -1) -> np.ndarray:
    return np.asarray(np.percentile(x, 50, axis=axis))


def _stat_p95(x: np.ndarray, axis: int = -1) -> np.ndarray:
    return np.asarray(np.percentile(x, 95, axis=axis))


def _stat_p99(x: np.ndarray, axis: int = -1) -> np.ndarray:
    return np.asarray(np.percentile(x, 99, axis=axis))


def _statistic_fn(statistic: Statistic) -> StatFn:
    match statistic:
        case Statistic.MEAN:
            return _stat_mean
        case Statistic.P50:
            return _stat_p50
        case Statistic.P95:
            return _stat_p95
        case Statistic.P99:
            return _stat_p99
        case _:
            assert_never(statistic)


def bca_paired_bootstrap_delta(
    baseline_per_prompt: list[float],
    candidate_per_prompt: list[float],
    *,
    statistic: Statistic,
    n_resamples: int = BOOTSTRAP_ITERATIONS,
    confidence_level: float = CONFIDENCE_LEVEL,
    rng_seed: int = BOOTSTRAP_SEED,
) -> PairedDelta:
    assert len(baseline_per_prompt) == len(candidate_per_prompt), (
        "baseline and candidate arrays must have equal length"
    )
    assert len(baseline_per_prompt) > 0, "input arrays must be non-empty"

    arr_baseline = np.asarray(baseline_per_prompt, dtype=float)
    arr_candidate = np.asarray(candidate_per_prompt, dtype=float)
    # Sign convention: positive Δ = candidate slower than baseline.
    deltas = arr_candidate - arr_baseline
    n_pairs = int(deltas.size)
    stat_fn = _statistic_fn(statistic)
    point = float(stat_fn(deltas, -1))

    if n_pairs < 2:
        return PairedDelta(
            statistic=statistic,
            delta=point,
            lower=point,
            upper=point,
            confidence_level=confidence_level,
            n_pairs=n_pairs,
            n_resamples=n_resamples,
        )

    rng = np.random.default_rng(seed=rng_seed)

    def paired_statistic(a: np.ndarray, b: np.ndarray, axis: int = -1) -> np.ndarray:
        # a corresponds to baseline_per_prompt, b corresponds to candidate_per_prompt.
        diffs = b - a
        return stat_fn(diffs, axis)

    try:
        res = bootstrap(
            data=(arr_baseline, arr_candidate),
            statistic=paired_statistic,
            n_resamples=n_resamples,
            confidence_level=confidence_level,
            method="BCa",
            paired=True,
            random_state=rng,
            vectorized=True,
        )
        lower = float(res.confidence_interval.low)
        upper = float(res.confidence_interval.high)
    except (RuntimeError, ValueError, FloatingPointError):
        # Degenerate inputs (all-equal deltas, zero variance) trip BCa's
        # acceleration-constant computation. Fall back to a degenerate CI.
        lower = point
        upper = point
    if not np.isfinite(lower) or not np.isfinite(upper):
        # Newer scipy versions return NaN bounds instead of raising on degenerate
        # data. Treat those as the same fallback case.
        lower = point
        upper = point

    return PairedDelta(
        statistic=statistic,
        delta=point,
        lower=lower,
        upper=upper,
        confidence_level=confidence_level,
        n_pairs=n_pairs,
        n_resamples=n_resamples,
    )
