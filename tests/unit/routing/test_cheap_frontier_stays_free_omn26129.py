# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129: cheap_frontier stays a free tier, and every rung on it is free today.

The tier is declared free_local at 0.0, so a paid model on it would be booked at $0
(the silent-paid path OMN-14224 and OMN-14225 closed). Paid OpenRouter models join
this ladder only after the code eval scores them (RULING 2026-11-11T12:33:37Z,
OMN-23751). Each rung is checked against the current provider-catalogue capture: it
must be listed, and listed at 0/0.
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

TIER = "cheap_frontier"
CAPTURE = Path(__file__).parents[2] / "fixtures" / "openrouter_models_2026_11_20.json"
ROUTING_TIERS = BIFROST_CONTRACT_PATH.parent / "routing_tiers.yaml"
# Paid families the operator named on 2026-11-20; they reach this tier only through
# the code eval, never by a direct placement.
EVAL_ONLY_PREFIXES = ("deepseek/", "qwen/qwen3.8")


def _rungs(contract_path: Path = BIFROST_CONTRACT_PATH) -> dict[str, str]:
    return {
        backend_id: str(backend["model_name"])
        for backend_id, backend in declared_backends(contract_path).items()
        if backend.get("tier") == TIER and backend.get("endpoint_url")
    }


def _capture() -> dict[str, dict[str, Any]]:
    payload = json.loads(CAPTURE.read_text(encoding="utf-8"))
    return {row["id"]: row for row in payload["data"]}


def _priced_or_unlisted(rungs: dict[str, str]) -> dict[str, str]:
    capture = _capture()
    bad: dict[str, str] = {}
    for backend_id, model in rungs.items():
        row = capture.get(model)
        if row is None:
            bad[backend_id] = f"{model}: not in the 2026-11-20 catalogue capture"
            continue
        pricing = row.get("pricing", {})
        if float(pricing.get("prompt", 1)) or float(pricing.get("completion", 1)):
            bad[backend_id] = (
                f"{model}: priced {pricing.get('prompt')}/{pricing.get('completion')}"
            )
    return bad


def test_the_tier_is_declared_free() -> None:
    tiers = yaml.safe_load(ROUTING_TIERS.read_text(encoding="utf-8"))["tiers"]
    (tier,) = [t for t in tiers if t["name"] == TIER]
    assert tier["cost_per_1k_tokens"] == 0.0
    assert tier["cost"]["cost_type"] == "free_local"


def test_every_rung_of_the_tier_is_listed_free_in_the_current_catalogue() -> None:
    rungs = _rungs()
    assert rungs, "cheap_frontier has no rung; the routing-completeness gates need one"
    assert not _priced_or_unlisted(rungs)


def test_no_eval_only_model_is_placed_on_the_tier() -> None:
    assert not [
        model for model in _rungs().values() if model.startswith(EVAL_ONLY_PREFIXES)
    ]


def test_the_checks_catch_a_paid_rung_and_an_unlisted_rung(tmp_path: Path) -> None:
    contract = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["backends"].extend(
        [
            {
                "backend_id": "paid-rung",
                "provider": "openrouter",
                "model_name": "deepseek/deepseek-v4-flash-0731",
                "tier": TIER,
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            },
            {
                "backend_id": "gone-rung",
                "provider": "openrouter",
                "model_name": "cohere/north-mini-code:free",
                "tier": TIER,
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            },
        ]
    )
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    rungs = _rungs(path)
    bad = _priced_or_unlisted(rungs)
    assert set(bad) >= {"paid-rung", "gone-rung"}
    assert any(model.startswith(EVAL_ONLY_PREFIXES) for model in rungs.values())
