# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The savings baseline when the caller named no session model.

``resolve_baseline_model`` takes the session model first. Without one, the
baseline is the configured ``pricing.baseline_model`` (the overlay, then the
store), then the model the developer has used most in the history the caller
passes, and only with none of those the fixed default. The history is a
mapping of model id to use count; a blank id or a count below one is not use.
A tie goes to the lowest model id, so the same history always names the same
baseline.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

__all__ = ["BaselineSelectionCase", "configured_or_most_used"]

#: How a run's savings baseline was chosen; the receipt's ``baseline_source``.
BaselineSelectionCase = Literal[
    "session_model", "overlay", "store", "most_used", "fixed_default"
]


def _configured(config: object) -> str | None:
    if not isinstance(config, dict):
        return None
    pricing_config = config.get("pricing")
    if not isinstance(pricing_config, dict):
        return None
    candidate = pricing_config.get("baseline_model")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None


def _most_used(history: Mapping[str, int] | None) -> str | None:
    counts = {
        model.strip(): count
        for model, count in (history or {}).items()
        if isinstance(model, str) and model.strip() and count >= 1
    }
    if not counts:
        return None
    return min(counts, key=lambda model: (-counts[model], model))


def configured_or_most_used(
    overlay: object, store: object, history: Mapping[str, int] | None
) -> tuple[str, BaselineSelectionCase] | None:
    """The baseline and its case, or None when the fixed default applies."""
    for config, case in ((overlay, "overlay"), (store, "store")):
        model = _configured(config)
        if model is not None:
            return model, "overlay" if case == "overlay" else "store"
    most_used = _most_used(history)
    return (most_used, "most_used") if most_used is not None else None
