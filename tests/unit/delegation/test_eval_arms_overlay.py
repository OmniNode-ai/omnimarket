# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23751: eval arms in the routing overlay stay pinned-only and under the spend cap."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_eval_arms_overlay import ModelEvalArmsOverlay

pytestmark = pytest.mark.unit

ENDPOINT = "https://hosted.example/api/v1/chat/completions"


def _backend(backend_id: str, slug: str) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "provider": "hosted",
        "endpoint_url": ENDPOINT,
        "model_name": slug,
        "secret_ref": "llm.hosted.api_key",
        "tier": "cheap_cloud",
        "timeout_ms": 300000,
        "max_tokens": 32768,
        "explicit_pin_only": True,
        "surface": "internal",
        "tenant_scope": "house",
        "capabilities": ["code_generation"],
    }


def _arm(backend_id: str, slug: str) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "slug": slug,
        "tier": "cheap_cloud",
        "price_in_usd_per_mtok": "0.10",
        "price_out_usd_per_mtok": "1.00",
        "context_length": 262144,
        "task_classes": ["code_generation", "refactor"],
        "catalog_read_at": "2026-11-11T12:36:49Z",
    }


def _eval_arms() -> dict[str, Any]:
    return {
        "spend_cap_usd": "25",
        "backends": [_backend("eval-a", "vendor/model-a")],
        "arms": [_arm("eval-a", "vendor/model-a")],
    }


def _overlay(eval_arms: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "delegation_routing_overlay.v1", "eval_arms": eval_arms}


def test_valid_eval_arms_load_through_the_routing_overlay() -> None:
    overlay = ModelDelegationRoutingOverlay.model_validate(_overlay(_eval_arms()))
    assert overlay.eval_arms is not None
    rows = overlay.eval_backend_rows()
    assert [r["backend_id"] for r in rows] == ["eval-a"]
    assert rows[0]["explicit_pin_only"] is True
    assert overlay.eval_arm_price_per_1m(ENDPOINT, "vendor/model-a") == (
        Decimal("0.10"),
        Decimal("1.00"),
    )
    assert overlay.eval_arm_price_per_1m(ENDPOINT, "vendor/other") is None


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("explicit_pin_only", False, "must be explicit_pin_only"),
        ("surface", "any", "surface must be internal"),
        ("tenant_scope", "any", "tenant_scope must be house"),
        ("model_name", "vendor/model-b", "differs from backend model_name"),
        (
            "capabilities",
            ["code_generation", "judge_adequacy"],
            "its arm does not list",
        ),
    ],
)
def test_a_backend_that_could_reach_real_work_is_refused(
    field: str, value: object, error: str
) -> None:
    data = _eval_arms()
    data["backends"][0][field] = value
    with pytest.raises(ValidationError, match=error):
        ModelEvalArmsOverlay.model_validate(data)


def test_a_backend_with_a_ladder_placement_is_refused() -> None:
    data = _eval_arms()
    data["backends"][0]["placement"] = {
        "tier": "cheap_cloud",
        "use_for": ["code_generation"],
    }
    with pytest.raises(ValidationError, match="placement"):
        ModelEvalArmsOverlay.model_validate(data)


def test_an_arm_without_a_backend_and_a_backend_without_an_arm_are_refused() -> None:
    data = _eval_arms()
    data["arms"][0]["backend_id"] = "eval-z"
    data["arms"][0]["slug"] = "vendor/model-a"
    with pytest.raises(ValidationError) as excinfo:
        ModelEvalArmsOverlay.model_validate(data)
    assert "has no arm" in str(excinfo.value)
    assert "undeclared backend 'eval-z'" in str(excinfo.value)


def test_an_eval_backend_may_not_reuse_a_harness_backend_id() -> None:
    data = _overlay(_eval_arms())
    data["harness_backends"] = [
        {
            "backend_id": "eval-a",
            "provider": "openai",
            "kind": "harness",
            "harness": "codex",
            "surface": "internal",
            "tenant_scope": "house",
            "explicit_pin_only": True,
            "tier": "harness",
            "timeout_ms": 600000,
            "max_tokens": 65536,
            "capabilities": ["code_review"],
        }
    ]
    with pytest.raises(ValidationError, match="reuses a harness backend_id"):
        ModelDelegationRoutingOverlay.model_validate(data)


def test_admit_run_admits_a_run_under_the_remaining_cap() -> None:
    arms = ModelEvalArmsOverlay.model_validate(_eval_arms())
    # 1,000,000 in at 0.10 plus 1,000,000 out at 1.00 is 1.10 USD.
    admission = arms.admit_run("eval-a", 1_000_000, 1_000_000, Decimal("20"))
    assert admission.admitted is True
    assert admission.projected_cost_usd == Decimal("1.1")
    assert admission.remaining_usd == Decimal("5")


def test_admit_run_refuses_a_run_over_the_remaining_cap() -> None:
    arms = ModelEvalArmsOverlay.model_validate(_eval_arms())
    admission = arms.admit_run("eval-a", 1_000_000, 1_000_000, Decimal("24.5"))
    assert admission.admitted is False
    assert "exceeds the 0.5 USD left of the 25 USD eval spend cap" in admission.reason


def test_admit_run_refuses_an_exhausted_cap_and_an_unknown_arm() -> None:
    arms = ModelEvalArmsOverlay.model_validate(_eval_arms())
    spent = arms.admit_run("eval-a", 1, 1, Decimal("30"))
    assert spent.admitted is False
    assert spent.remaining_usd == Decimal("0")
    unknown = arms.admit_run("local-coder", 0, 0, Decimal("0"))
    assert unknown.admitted is False
    assert "is not an eval arm" in unknown.reason


def test_admit_run_refuses_negative_inputs() -> None:
    arms = ModelEvalArmsOverlay.model_validate(_eval_arms())
    with pytest.raises(ValueError, match="spent_usd"):
        arms.admit_run("eval-a", 1, 1, Decimal("-1"))
    with pytest.raises(ValueError, match="token counts"):
        arms.admit_run("eval-a", -1, 1, Decimal("0"))


def test_the_spend_cap_must_be_positive() -> None:
    data = copy.deepcopy(_eval_arms())
    data["spend_cap_usd"] = "0"
    with pytest.raises(ValidationError, match="spend_cap_usd"):
        ModelEvalArmsOverlay.model_validate(data)
