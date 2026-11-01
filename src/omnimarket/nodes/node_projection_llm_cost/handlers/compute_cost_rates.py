"""Compute-cost rate loader for the llm_call_metrics projection.

Reads the per-GPU-type ``electricity_per_hour`` and ``amortization_per_hour``
rates from the pricing manifest so HandlerProjectionLlmCost can fold compute
cost into the persisted ``estimated_cost_usd``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_compute_cost_rates(
    pricing_manifest_path: Path | None,
) -> dict[str, dict[str, float]]:
    """Return the compute-cost rates by GPU type, or ``{}`` when none are readable."""
    if pricing_manifest_path is None or not pricing_manifest_path.exists():
        return {}

    raw = yaml.safe_load(pricing_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    raw_compute_cost = raw.get("compute_cost", {})
    if not isinstance(raw_compute_cost, dict):
        return {}

    rates: dict[str, dict[str, float]] = {}
    for gpu_type, entry in raw_compute_cost.items():
        if not isinstance(gpu_type, str) or not isinstance(entry, dict):
            continue
        electricity = _float_or_none(entry.get("electricity_per_hour"))
        amortization = _float_or_none(entry.get("amortization_per_hour"))
        if electricity is None or amortization is None:
            continue
        rates[gpu_type] = {
            "electricity_per_hour": electricity,
            "amortization_per_hour": amortization,
        }
    return rates


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["load_compute_cost_rates"]
