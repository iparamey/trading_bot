"""Auto-detection of support/resistance levels from D1 bars."""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from mt5_utils import get_daily_rates, pips_to_price


@dataclass
class AutoLevelsResult:
    """Detected levels with auxiliary diagnostics."""

    levels: list[float]
    fractal_highs: list[float]
    fractal_lows: list[float]
    pivots: list[float]


def _fractal_levels(highs: np.ndarray, lows: np.ndarray) -> tuple[list[float], list[float]]:
    """Find simple 5-candle fractal highs/lows."""
    f_highs: list[float] = []
    f_lows: list[float] = []
    if len(highs) < 5:
        return f_highs, f_lows

    for i in range(2, len(highs) - 2):
        center_h = highs[i]
        center_l = lows[i]
        if center_h > highs[i - 1] and center_h > highs[i - 2] and center_h > highs[i + 1] and center_h > highs[i + 2]:
            f_highs.append(float(center_h))
        if center_l < lows[i - 1] and center_l < lows[i - 2] and center_l < lows[i + 1] and center_l < lows[i + 2]:
            f_lows.append(float(center_l))
    return f_highs, f_lows


def _daily_pivots(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> list[float]:
    """Compute classic pivot levels PP/R1/S1 for each day (from previous day)."""
    pivots: list[float] = []
    if len(highs) < 2:
        return pivots
    for i in range(1, len(highs)):
        h, l, c = highs[i - 1], lows[i - 1], closes[i - 1]
        pp = (h + l + c) / 3
        r1 = 2 * pp - l
        s1 = 2 * pp - h
        pivots.extend([float(pp), float(r1), float(s1)])
    return pivots


def _cluster_levels(raw_levels: list[float], min_distance: float) -> list[float]:
    """Deduplicate nearby levels by minimum distance threshold."""
    if not raw_levels:
        return []
    sorted_levels = sorted(raw_levels)
    clustered: list[float] = [sorted_levels[0]]
    for level in sorted_levels[1:]:
        if abs(level - clustered[-1]) >= min_distance:
            clustered.append(level)
    return clustered


def detect_levels(symbol: str, lookback_days: int, min_distance_pips: float = 10.0) -> AutoLevelsResult:
    """Detect support/resistance levels from D1 bars for lookback window."""
    rates = get_daily_rates(symbol=symbol, lookback_days=max(lookback_days, 30))
    if not rates:
        return AutoLevelsResult(levels=[], fractal_highs=[], fractal_lows=[], pivots=[])

    highs = np.array([r["high"] for r in rates], dtype=float)
    lows = np.array([r["low"] for r in rates], dtype=float)
    closes = np.array([r["close"] for r in rates], dtype=float)

    f_highs, f_lows = _fractal_levels(highs, lows)
    pivots = _daily_pivots(highs, lows, closes)
    min_distance = pips_to_price(symbol, min_distance_pips)
    all_levels = _cluster_levels(f_highs + f_lows + pivots, min_distance=min_distance)

    return AutoLevelsResult(
        levels=all_levels,
        fractal_highs=_cluster_levels(f_highs, min_distance=min_distance),
        fractal_lows=_cluster_levels(f_lows, min_distance=min_distance),
        pivots=_cluster_levels(pivots, min_distance=min_distance),
    )
