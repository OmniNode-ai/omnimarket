# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract names no cheap_frontier rung whose last recorded smoke failed.

A rung is smoked on the same ten escalated tasks through ``onex delegate``
in-process, graded by the delegate contract's own acceptance check. It
qualifies at the recorded bar of accepted answers out of ten. Every rung the
ordinary ladder can reach, a spread peer included, needs a recorded smoke that
meets the bar; a rung parked as explicit-pin-only is not on the ladder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    declared_backends,
)

pytestmark = pytest.mark.unit
# Dated measurements remain covered by the repository's retired-model gate.
# Retired identifiers are redacted; measured counts and verdicts are preserved.
FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "receipts"
    / "cheap_frontier_smokes_2026_10_25.json"
)
TIER = "cheap_frontier"


def _smokes() -> dict[str, dict[str, Any]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {row["model"]: row for row in payload["smokes"]}


def _bar() -> int:
    return int(json.loads(FIXTURE.read_text(encoding="utf-8"))["bar_accepted_of_10"])


def _ladder_rungs(contract_path: Path = BIFROST_CONTRACT_PATH) -> dict[str, str]:
    """Backend id to served model for every ordinary rung of the tier."""
    return {
        backend_id: str(backend["model_name"])
        for backend_id, backend in declared_backends(contract_path).items()
        if backend.get("tier") == TIER
        and backend.get("endpoint_url")
        and not backend.get("explicit_pin_only", False)
    }


def _failed_or_unsmoked(
    rungs: dict[str, str], smokes: dict[str, dict[str, Any]], bar: int
) -> dict[str, str]:
    bad: dict[str, str] = {}
    for backend_id, model in rungs.items():
        smoke = smokes.get(model)
        if smoke is None:
            bad[backend_id] = f"{model}: no recorded smoke"
        elif smoke["accepted"] < bar:
            bad[backend_id] = f"{model}: accepted {smoke['accepted']} of 10, bar {bar}"
    return bad


def test_the_contract_names_no_rung_whose_last_smoke_failed() -> None:
    assert not _failed_or_unsmoked(_ladder_rungs(), _smokes(), _bar())


def test_the_check_flags_a_rung_below_the_bar_and_a_rung_never_smoked(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["backends"].extend(
        [
            {
                "backend_id": "weak-rung",
                "provider": "openrouter",
                "model_name": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "tier": TIER,
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            },
            {
                "backend_id": "unsmoked-rung",
                "provider": "openrouter",
                "model_name": "vendor/never-smoked",
                "tier": TIER,
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            },
        ]
    )
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    bad = _failed_or_unsmoked(_ladder_rungs(path), _smokes(), _bar())
    assert set(bad) >= {"weak-rung", "unsmoked-rung"}


def test_a_parked_rung_is_not_on_the_ladder(tmp_path: Path) -> None:
    contract = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["backends"].append(
        {
            "backend_id": "parked-rung",
            "provider": "openrouter",
            "model_name": "vendor/parked",
            "tier": TIER,
            "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            "explicit_pin_only": True,
        }
    )
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    assert "parked-rung" not in _ladder_rungs(path)
