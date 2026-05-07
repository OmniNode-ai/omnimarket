# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Aggregator for the ADK evaluation measurement harness (P8).

Joins per-track self-reported latency/token metrics (track_a_metrics.json,
track_b_metrics.json) with scorer output (scores.json) and emits a single
flat dict suitable for `measurements.json`.

Cost semantics
--------------
Track A cost is pulled from `estimated_cost_usd_per_run_median` in
track_a_metrics.json (AI Studio Gemini Flash pricing, computed by the
Track A worker). Track B is a local Qwen3-Coder on .201 — the marginal
per-run inference cost is ~$0; hardware/power/operator are NOT included.

Dev-time semantics
------------------
`dev_time_minutes_a` / `dev_time_minutes_b` are passed in by the caller
(the `__main__.py` CLI hardcodes the rough wall-clock from worker
dispatch logs). These are directional only and carry a caveat in the
output.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _median_tokens(values: list[int]) -> float:
    """Sample median for an int token list (matches Track A's convention)."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def aggregate(
    track_a_metrics_path: Path,
    track_b_metrics_path: Path,
    scores_path: Path,
    dev_time_minutes_a: int,
    dev_time_minutes_b: int,
) -> dict[str, Any]:
    """Join track metrics + scores into a single measurements dict.

    All floats are rounded to 6 decimal places on emit so downstream
    diffs don't churn on float precision noise.
    """
    track_a_metrics = _read_json(track_a_metrics_path)
    track_b_metrics = _read_json(track_b_metrics_path)
    scores = _read_json(scores_path)

    a_latency = float(track_a_metrics["latency_seconds_median"])
    b_latency = float(track_b_metrics["latency_median_seconds"])
    a_cost = float(track_a_metrics["estimated_cost_usd_per_run_median"])
    b_cost = 0.0

    a_binary_f1 = float(scores["track_a"]["binary_f1"])
    a_macro_f1 = float(scores["track_a"]["macro_f1"])
    b_binary_f1 = float(scores["track_b"]["binary_f1"])
    b_macro_f1 = float(scores["track_b"]["macro_f1"])

    a_input_median = _median_tokens(track_a_metrics["input_tokens_per_run"])
    a_output_median = _median_tokens(track_a_metrics["output_tokens_per_run"])

    latency_b_over_a = b_latency / a_latency if a_latency > 0 else 0.0
    dev_time_a_over_b = (
        dev_time_minutes_a / dev_time_minutes_b if dev_time_minutes_b > 0 else 0.0
    )
    binary_f1_gap_pp = abs(a_binary_f1 - b_binary_f1) * 100.0

    track_a_block: dict[str, Any] = {
        "model": str(track_a_metrics["model"]),
        "auth_path": str(track_a_metrics["auth_path"]),
        "latency_seconds_median": round(a_latency, 6),
        "cost_usd_per_run_median": round(a_cost, 6),
        "dev_time_minutes_rough": dev_time_minutes_a,
        "binary_f1": round(a_binary_f1, 6),
        "macro_f1": round(a_macro_f1, 6),
        "tokens_input_median": a_input_median,
        "tokens_output_median": a_output_median,
    }

    track_b_block: dict[str, Any] = {
        "model": f"Qwen3-Coder-30B ({track_b_metrics['base_url']})",
        "transport": "curl shellout (macOS firewall workaround)",
        "latency_seconds_median": round(b_latency, 6),
        "cost_usd_per_run_median": round(b_cost, 6),
        "cost_note": (
            "Marginal inference cost only; excludes hardware/power/operator cost. "
            "Local GPU already paid for."
        ),
        "dev_time_minutes_rough": dev_time_minutes_b,
        "binary_f1": round(b_binary_f1, 6),
        "macro_f1": round(b_macro_f1, 6),
    }

    ratios_block: dict[str, Any] = {
        "latency_b_over_a": round(latency_b_over_a, 6),
        "cost_a_over_b_marginal_note": (
            f"Track A ~${a_cost:.4f}/run, Track B marginal ~= $0. "
            "Cost ratio is functionally infinite at marginal scope."
        ),
        "dev_time_a_over_b": round(dev_time_a_over_b, 6),
        "binary_f1_gap_pp": round(binary_f1_gap_pp, 6),
    }

    caveats: list[str] = [
        "Dev-time is rough wall-clock from single-developer spike, not rigorous TCO.",
        "Cost is marginal per-run inference only; excludes hardware/power/operator.",
        "F1 numbers use LLM-self-labeled P5 sample (N=30, 28 unique); directional only.",
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "track_a": track_a_block,
        "track_b": track_b_block,
        "ratios": ratios_block,
        "caveats": caveats,
    }


__all__ = ["aggregate"]
