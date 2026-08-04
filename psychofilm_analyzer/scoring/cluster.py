"""Psychological cluster assignment."""

from __future__ import annotations

from typing import Optional


def assign_clusters(
    cluster_scores: dict[str, float],
    min_primary: float = 1.0,
    min_secondary: float = 1.0,
    secondary_ratio: float = 0.45,
) -> tuple[Optional[str], Optional[str]]:
    if not cluster_scores:
        return None, None
    ranked = sorted(cluster_scores.items(), key=lambda kv: kv[1], reverse=True)
    primary_name, primary_val = ranked[0]
    if primary_val < min_primary:
        # fallback: still assign top if any signal, else None
        if primary_val <= 0:
            return None, None
    primary = primary_name
    secondary = None
    if len(ranked) > 1:
        sec_name, sec_val = ranked[1]
        if sec_val >= min_secondary and sec_val >= primary_val * secondary_ratio:
            secondary = sec_name
    return primary, secondary
