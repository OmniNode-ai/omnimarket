# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20833 AC1: omnimarket installs the omnibase_infra release carrying #4631.

omnibase_infra 0.38.70 is the first release whose pricing manifest carries the
providers' published rates, including claude-opus-4-6 at $5 / $25 per MTok
rather than the retired $15 / $75.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

REPO_ROOT = Path(__file__).resolve().parents[3]
FIRST_RELEASE_WITH_PUBLISHED_RATES = (0, 38, 70)


def _locked_infra_version() -> tuple[int, ...]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    (version,) = [
        package["version"]
        for package in lock["package"]
        if package["name"] == "omnibase-infra"
    ]
    return tuple(int(part) for part in version.split("."))


def test_the_lock_resolves_omnibase_infra_to_a_release_carrying_4631() -> None:
    assert _locked_infra_version() >= FIRST_RELEASE_WITH_PUBLISHED_RATES


def test_the_installed_manifest_carries_the_published_opus_rate() -> None:
    entry = ModelPricingTable.from_yaml().get_entry("claude-opus-4-6")
    assert entry is not None
    assert entry.source == "PROVIDER_PUBLISHED_PRICING"
    assert (entry.input_cost_per_1k, entry.output_cost_per_1k) == (0.005, 0.025)
