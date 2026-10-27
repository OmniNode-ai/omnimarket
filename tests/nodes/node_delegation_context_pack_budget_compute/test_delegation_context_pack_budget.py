# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21119 (TA.18 AC1, AC3): the delegation orchestrator's context-pack budget and admission.

AC1: the pack's budget is the routed backend's grounded-input ceiling minus the request
composed without the pack, never above the profile's budget; a request with no room left is
sent without a pack and records ``no_room``. AC3: a task class naming a profile is dispatched
only with a non-empty pack hash; an empty hash is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.pack as pack_package
from omnimarket.inference.task_class_authority import (
    resolve_task_class_context_profile,
)
from omnimarket.nodes.node_delegation_context_pack_budget_compute.handlers.handler_delegation_context_pack_budget import (
    HandlerDelegationContextPackBudget,
)
from omnimarket.nodes.node_delegation_context_pack_budget_compute.models.model_delegation_context_pack_budget import (
    EnumDelegationContextPackStatus,
    ModelDelegationContextPackBudgetRequest,
    ModelDelegationContextPackBudgetResult,
)

pytestmark = pytest.mark.unit

PROFILE = "delegation_payload_local"
BASE: dict[str, Any] = {
    "correlation_id": "corr-21119",
    "task_class": "fixture_delegation_payload",
    "context_profile": PROFILE,
    "profile_token_budget": 8000,
    "composed_request_tokens": 3000,
    "max_grounded_input_tokens": 8000,
    "token_estimator": "qwen3-tokenizer",
}


def _decide(**overrides: Any) -> ModelDelegationContextPackBudgetResult:
    request = ModelDelegationContextPackBudgetRequest.model_validate(
        {**BASE, **overrides}
    )
    return HandlerDelegationContextPackBudget().handle(request)


def test_a_class_naming_no_profile_dispatches_without_a_pack() -> None:
    result = _decide(context_profile=None, profile_token_budget=None)
    assert result.status is EnumDelegationContextPackStatus.NOT_REQUESTED
    assert result.dispatch_allowed is True
    assert result.token_budget_override is None
    assert result.context_pack_hash == ""


def test_the_pack_budget_is_the_room_left_under_the_ceiling() -> None:
    result = _decide()
    assert result.status is EnumDelegationContextPackStatus.REQUEST_PACK
    assert result.token_budget_override == 5000
    assert result.dispatch_allowed is False


def test_the_pack_budget_never_exceeds_the_profile_budget() -> None:
    result = _decide(max_grounded_input_tokens=32000, composed_request_tokens=1000)
    assert result.status is EnumDelegationContextPackStatus.REQUEST_PACK
    assert result.token_budget_override == 8000


@pytest.mark.parametrize("composed", [8000, 8001, 20000])
def test_a_request_with_no_room_is_sent_without_a_pack(composed: int) -> None:
    result = _decide(composed_request_tokens=composed)
    assert result.status is EnumDelegationContextPackStatus.NO_ROOM
    assert result.dispatch_allowed is True
    assert result.token_budget_override is None
    assert result.context_pack_hash == ""


def test_one_token_of_room_still_requests_a_pack() -> None:
    result = _decide(composed_request_tokens=7999)
    assert result.status is EnumDelegationContextPackStatus.REQUEST_PACK
    assert result.token_budget_override == 1


def test_a_built_pack_with_a_hash_is_attached_and_dispatched() -> None:
    result = _decide(
        context_pack_hash="sha256:" + "a" * 64, assembled_request_tokens=7500
    )
    assert result.status is EnumDelegationContextPackStatus.ATTACH
    assert result.dispatch_allowed is True
    assert result.context_pack_hash == "sha256:" + "a" * 64
    assert result.token_budget_override == 5000


@pytest.mark.parametrize("empty", ["", "   "])
def test_a_pack_answer_with_an_empty_hash_is_refused(empty: str) -> None:
    result = _decide(context_pack_hash=empty)
    assert result.status is EnumDelegationContextPackStatus.REFUSE_EMPTY_HASH
    assert result.dispatch_allowed is False
    assert result.context_pack_hash == ""
    assert "corr-21119" in result.reason


def test_no_room_wins_over_a_pack_answer() -> None:
    # A request already over the ceiling is never sent with a pack, whatever arrived.
    result = _decide(
        composed_request_tokens=9000, context_pack_hash="sha256:" + "b" * 64
    )
    assert result.status is EnumDelegationContextPackStatus.NO_ROOM
    assert result.context_pack_hash == ""


def test_a_class_with_no_profile_never_carries_a_hash() -> None:
    result = _decide(
        context_profile=None,
        profile_token_budget=None,
        context_pack_hash="sha256:" + "c" * 64,
    )
    assert result.status is EnumDelegationContextPackStatus.NOT_REQUESTED
    assert result.context_pack_hash == ""


def test_the_result_names_the_estimator_and_echoes_the_identity() -> None:
    result = _decide()
    assert result.token_estimator == "qwen3-tokenizer"
    assert result.correlation_id == "corr-21119"
    assert result.task_class == "fixture_delegation_payload"
    assert result.context_profile == PROFILE
    assert result.composed_request_tokens == 3000
    assert result.max_grounded_input_tokens == 8000


@pytest.mark.parametrize(
    "overrides",
    [
        {"correlation_id": ""},
        {"task_class": ""},
        {"token_estimator": ""},
        {"composed_request_tokens": -1},
        {"max_grounded_input_tokens": 0},
        {"profile_token_budget": 0},
        # A profile with no budget cannot be bounded.
        {"profile_token_budget": None},
        # A budget with no profile is a caller error, not a silent pack.
        {"context_profile": None},
        {"context_profile": "Not-A-Profile"},
        {"unexpected": 1},
    ],
)
def test_malformed_requests_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationContextPackBudgetRequest.model_validate({**BASE, **overrides})


def test_models_are_frozen_and_round_trip() -> None:
    result = _decide()
    assert (
        ModelDelegationContextPackBudgetResult.model_validate_json(
            result.model_dump_json()
        )
        == result
    )
    for model in (
        ModelDelegationContextPackBudgetRequest,
        ModelDelegationContextPackBudgetResult,
    ):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"


def test_the_shipped_profile_budget_is_the_cap_the_compute_applies() -> None:
    # The profile file is the budget's source of truth; the fixture above uses its value.
    profile_file = Path(pack_package.__file__).parent / "profiles" / f"{PROFILE}.yaml"
    profile = yaml.safe_load(profile_file.read_text(encoding="utf-8"))
    assert profile["token_budget"] == BASE["profile_token_budget"]


def test_no_production_task_class_requests_a_pack_yet() -> None:
    # The wiring stays dark until TC.13 promotes the profile on measured evidence.
    for task_class in ("document", "code_generation", "reasoning", "research"):
        result = _decide(
            task_class=task_class,
            context_profile=resolve_task_class_context_profile(task_class),
            profile_token_budget=None,
        )
        assert result.status is EnumDelegationContextPackStatus.NOT_REQUESTED


def test_pack_hash_requires_a_full_input_measurement() -> None:
    result = _decide(context_pack_hash="sha256:" + "d" * 64)
    assert result.status.value == "refuse_unmeasured_input"
    assert result.dispatch_allowed is False
    assert result.context_pack_hash == ""


@pytest.mark.parametrize("assembled", [8001, 12000])
def test_a_pack_cannot_authorize_an_outbound_input_above_the_routed_ceiling(
    assembled: int,
) -> None:
    result = _decide(
        context_pack_hash="sha256:" + "e" * 64,
        assembled_request_tokens=assembled,
    )
    assert result.status.value == "refuse_input_overflow"
    assert result.dispatch_allowed is False
    assert result.context_pack_hash == ""
    assert result.assembled_request_tokens == assembled


def test_an_assembled_input_at_the_ceiling_is_admitted_and_records_its_count() -> None:
    result = _decide(
        context_pack_hash="sha256:" + "f" * 64,
        assembled_request_tokens=8000,
    )
    assert result.status is EnumDelegationContextPackStatus.ATTACH
    assert result.dispatch_allowed is True
    assert result.assembled_request_tokens == 8000


@pytest.mark.parametrize(
    "overrides",
    [
        {"assembled_request_tokens": -1},
        {"assembled_request_tokens": 2999, "context_pack_hash": "hash"},
        {"assembled_request_tokens": 4000},
    ],
)
def test_an_inconsistent_full_input_measurement_is_refused(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationContextPackBudgetRequest.model_validate({**BASE, **overrides})
