# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC3, follow-up: the model-routing page serves each tier model's spend from a named price only.

The review of #6442 (2026-11-18) found model-routing.v1's tier-model rows still summing the raw stored cost, so a
routing tier's flat rate on a model with no price was served as that model's cost on the routing page, the
original AC3 defect on another local page.

Failure modes, each with a test below:
  T1  a flat rate on a model the manifest does not list is served as the model's cost, and not counted unmeasured
  T2  a positive cost on a model the manifest lists as free is served as cost
  T3  a priced model's cost is dropped (over-exclusion)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _ROUTING,
    _row,
    _run,
    _store,
)

pytestmark = pytest.mark.unit


def _tier_models(path: Path) -> dict[str, dict[str, Any]]:
    by_tier = _row(path, _ROUTING)["by_tier"]
    return {
        model["model_name"]: model
        for tier in by_tier["tiers"]
        for model in tier["models"]
    }


def test_routing_priced_spend_serves_each_tier_model_from_a_named_price(
    tmp_path: Path,
) -> None:
    """T1, T2, T3: the AC3 falsifier on the model-routing page."""
    path = _store(
        tmp_path,
        _run(
            "priced",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
        ),
        _run(
            "unlisted",
            model_name="vendor/model:free",
            delegated_to="vendor/model:free",
            cost_usd=0.05,
        ),
        _run(
            "free-listed",
            model_name="qwen2.5-7b",
            delegated_to="qwen2.5-7b",
            cost_usd=0.05,
        ),
    )
    models = _tier_models(path)
    assert models["gpt-4o-mini"]["cost_usd"] == pytest.approx(0.0001)
    assert models["vendor/model:free"]["cost_usd"] is None
    assert models["vendor/model:free"]["unmeasured_cost_count"] == 1
    assert models["qwen2.5-7b"]["cost_usd"] is None
    assert models["qwen2.5-7b"]["unmeasured_cost_count"] == 1
