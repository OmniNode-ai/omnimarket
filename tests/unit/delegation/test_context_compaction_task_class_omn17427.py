# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``context_compaction`` task class (context service plan TA.11).

Compaction is a delegation task class: the model-written section of a
compaction package is routed through the chain of responders like any other
delegated task. The class is internal (the compaction orchestrator names it
explicitly) and is never resolved from prompt text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from omnimarket.inference.protocol_config import load_inference_protocol_config
from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    load_task_class_authority,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegation_request import (
    ModelDelegationRequest,
)

_TASK_CLASS = "context_compaction"
# The summarization-shaped profiles: a compaction call routed to one of these
# models gets the same request shaping as a summarization call.
_SHAPED_PROFILES = (
    "cloud-glm-no-think",
    "cloud-glm-5-3-thinking",
    "openrouter-reasoning-minimal-excluded",
)
_CONTRACTS = (
    Path(__file__).parents[3] / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)


def _entry() -> dict[str, Any]:
    document = yaml.safe_load(_CONTRACTS.read_text(encoding="utf-8"))
    entry = document["task_classes"][_TASK_CLASS]
    assert isinstance(entry, dict)
    return entry


@pytest.mark.unit
def test_context_compaction_is_an_internal_task_class() -> None:
    authority = load_task_class_authority()

    assert _TASK_CLASS in authority.universe
    assert _TASK_CLASS in authority.internal_task_classes
    assert authority.task_classes[_TASK_CLASS].gateway_exposure == (
        EnumGatewayExposure.INTERNAL
    )


@pytest.mark.unit
def test_context_compaction_is_never_resolved_from_prompt_text() -> None:
    selection = _entry()["selection"]

    assert selection["phrases"] == []


@pytest.mark.unit
def test_context_compaction_declares_an_execution_budget() -> None:
    budget = load_task_class_authority().execution_budget(_TASK_CLASS)

    assert budget.task_class_timeout_ceiling_seconds > 0
    assert budget.terminal_delivery_margin_seconds > 0


@pytest.mark.unit
def test_context_compaction_declares_a_ladder_it_can_reach() -> None:
    policy = _entry()["escalation_policy"]
    tier_order = policy["tier_order"]

    assert tier_order
    assert policy["max_escalations"] >= len(tier_order) - 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "request_model", [ModelDelegationRequest, ModelDelegateSkillRequest]
)
def test_delegation_request_models_accept_context_compaction(
    request_model: type[ModelDelegationRequest] | type[ModelDelegateSkillRequest],
) -> None:
    accepted = get_args(request_model.model_fields["task_type"].annotation)

    assert _TASK_CLASS in accepted


@pytest.mark.unit
@pytest.mark.parametrize("profile_id", _SHAPED_PROFILES)
def test_summarization_shaped_protocol_profiles_list_context_compaction(
    profile_id: str,
) -> None:
    config = load_inference_protocol_config()
    profile = next(p for p in config.profiles if p.profile_id == profile_id)

    assert "summarization" in profile.task_types
    assert profile.task_types.count(_TASK_CLASS) == 1
