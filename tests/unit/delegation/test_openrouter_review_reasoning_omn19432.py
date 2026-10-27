# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the free OpenRouter rung serves `review` and `reasoning`.

RULING 2026-09-30T15:09:11Z: every model is used as much as its measurement
earns. On the 30 delegated tasks of 2026-09-30 (judge off, the delegate's own
gate) nemotron-3-ultra passed 5 of 5 `review` and 3 of 3 `reasoning`, where the
local Qwen hosts passed 1 of 5 `review`. Both classes ended their ladder at the
metered tier, so a local miss went straight to a paid call. ``tier_order`` is a
CLOSED set, so the rung had to be named there, ahead of the metered tier, with
``max_escalations`` raised so the ladder can still be walked to its last entry.

The committed contracts are read as data. Each assertion has a control: the
same read for a class this change did not touch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.placed_routing_config import placed_tiers_raw

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_CLASSES = ("review", "reasoning")
_FREE_RUNG = "openrouter-north-mini-code"


def _task_classes() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (_CONFIGS / "task_class_contracts.v1.yaml").read_text(encoding="utf-8")
    )
    return dict(loaded["task_classes"])


def _tiers() -> dict[str, Any]:
    return placed_tiers_raw()


@pytest.mark.parametrize("task_class", _CLASSES)
def test_the_free_tier_sits_between_local_and_the_metered_tier(task_class: str) -> None:
    order = _task_classes()[task_class]["escalation_policy"]["tier_order"]
    assert order == ["local", "cheap_frontier", "cheap_cloud"]


@pytest.mark.parametrize("task_class", _CLASSES)
def test_the_ladder_can_reach_its_last_entry(task_class: str) -> None:
    policy = _task_classes()[task_class]["escalation_policy"]
    assert policy["max_escalations"] >= len(policy["tier_order"]) - 1


@pytest.mark.parametrize("task_class", _CLASSES)
def test_the_free_rung_declares_the_class(task_class: str) -> None:
    models = _tiers()["cheap_frontier"]["models"]
    rung = next(m for m in models if m["backend_id"] == _FREE_RUNG)
    assert task_class in rung["use_for"]


def test_the_free_tier_stays_free() -> None:
    tier = _tiers()["cheap_frontier"]
    assert tier["cost_per_1k_tokens"] == 0.0
    assert tier["cost"]["cost_type"] == "free_local"


def test_a_class_this_change_did_not_touch_keeps_its_ladder() -> None:
    """CONTROL: `planning` was not measured, so it gains no OpenRouter tier."""
    order = _task_classes()["planning"]["escalation_policy"]["tier_order"]
    assert "cheap_frontier" not in order


_SIBLING = "openrouter-nemotron-super"
_ULTRA = "openrouter-nemotron-ultra"


def _bifrost_backends() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (_CONFIGS / "bifrost_delegation.yaml").read_text(encoding="utf-8")
    )
    return {b["backend_id"]: b for b in loaded["backends"]}


def test_the_withdrawn_siblings_are_absent_from_the_free_tier() -> None:
    """Super is neither a tier entry nor a declared backend; Ultra is no tier entry (OMN-20376)."""
    refs = [m["backend_id"] for m in _tiers()["cheap_frontier"]["models"]]
    assert refs == [_FREE_RUNG]
    assert _SIBLING not in _bifrost_backends()
    assert _ULTRA not in refs


def test_the_free_rung_leaves_out_the_class_it_measured_weak_on() -> None:
    """north-mini-code passed 2 of 5 code_review tasks, so that class is not offered to it."""
    models = {m["backend_id"]: m for m in _tiers()["cheap_frontier"]["models"]}
    backend = _bifrost_backends()[_FREE_RUNG]
    assert backend["tier"] == "cheap_frontier"
    # OMN-20376: a standalone placement, its own use_for written out, no rung
    # to mirror.
    assert "fallback_for" not in backend["placement"]
    assert backend["placement"]["use_for"] == models[_FREE_RUNG]["use_for"]
    assert "code_review" not in models[_FREE_RUNG]["use_for"]
    assert "review" in models[_FREE_RUNG]["use_for"]


def test_only_free_slugs_are_named_on_the_free_tier() -> None:
    """CONTROL: a paid slug on this tier would spend money under a zero-cost label."""
    backends = _bifrost_backends()
    refs = [m["backend_id"] for m in _tiers()["cheap_frontier"]["models"]]
    assert refs
    for ref in refs:
        backend = backends[ref]
        assert backend["tier"] == "cheap_frontier"
        assert backend["provider"] == "openrouter"
        assert backend["model_name"].endswith(":free"), backend["model_name"]
        assert backend["secret_ref"] == "llm.openrouter.api_key"


def test_the_parked_ultra_backend_can_still_judge_adequacy() -> None:
    backend = _bifrost_backends()[_ULTRA]
    assert "judge_adequacy" in backend["capabilities"]
