# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane brief drafting is a task class on the lab Qwen rung only, gated by the brief checker (OMN-17427).

Experiment exp-brief-drafting-local (2026-10-17, h202): the lab Qwen rung drafts the free-text sections
of a lane brief, ``lane_brief.py check`` is the acceptance gate, the shell supplies the ticket-state
acknowledgements, the quoted criteria and the 3a block, and GLM is not usable for this class. These
tests read the class through the real routing functions, so an edit that keeps the letter of the
declaration and moves the route still fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.inference.task_class_authority import (
    EnumGatewayExposure,
    ModelExternalAcceptance,
    load_task_class_authority,
)
from omnimarket.models.lane_brief_draft.enum_brief_draft_slot import EnumBriefDraftSlot
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_TASK_CONTRACT = _ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
_ROUTING_TIERS = _ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_CLASS = "brief_drafting"


def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_TASK_CONTRACT.read_text())
    assert isinstance(raw, dict)
    return raw


def _entry() -> dict[str, object]:
    task_classes = _contract()["task_classes"]
    assert isinstance(task_classes, dict)
    entry = task_classes[_CLASS]
    assert isinstance(entry, dict)
    return entry


def test_class_is_internal_and_never_resolved_from_a_prompt() -> None:
    authority = load_task_class_authority()
    assert _CLASS in authority.universe
    assert _CLASS in authority.internal_task_classes
    assert _CLASS not in authority.public_task_classes
    entry = authority.task_classes[_CLASS]
    assert entry.gateway_exposure is EnumGatewayExposure.INTERNAL
    assert entry.selection.phrases == ()
    assert entry.routing_availability is None


def test_ladder_is_the_local_tier_alone_and_reaches_it() -> None:
    escalation = _entry()["escalation_policy"]
    assert isinstance(escalation, dict)
    assert escalation["tier_order"] == ["local"]
    assert escalation["max_escalations"] == 0
    config = parse_delegation_config_yaml(_ROUTING_TIERS.read_text())
    tiers = routing._tier_order_from_contract(
        config, routing._task_class_entry(_contract(), _CLASS)
    )
    assert [tier.name for tier in tiers] == ["local"]


def test_the_class_resolves_the_qwen_model_and_no_glm_backend_is_on_its_ladder() -> (
    None
):
    config = parse_delegation_config_yaml(_ROUTING_TIERS.read_text())
    entry = routing._task_class_entry(_contract(), _CLASS)
    (tier,) = routing._tier_order_from_contract(config, entry)
    backends = {
        model.backend_ref: routing.BifrostBackendRef(
            endpoint_url=f"https://{model.backend_ref}.contract.test/v1/chat/completions",
            model_name=model.id,
            timeout_ms=30_000,
            max_tokens=4096,
        )
        for model in tier.models
    }
    chosen = routing._select_model_for_task(
        tier.models, _CLASS, 0, backends, contract_model_ref=None
    )
    assert chosen is not None
    assert chosen.id.startswith("Qwen")
    assert not [m for m in tier.models if "glm" in f"{m.id} {m.backend_ref}".lower()]
    for other in config.tiers:
        if other.name == "local":
            continue
        # Positive control: the metered and frontier tiers do not serve the class either.
        assert (
            routing._select_model_for_task(
                other.models, _CLASS, 0, backends, contract_model_ref=None
            )
            is None
        ), other.name


def test_checker_is_the_declared_acceptance_gate_and_shell_parts_are_code_filled() -> (
    None
):
    entry = load_task_class_authority().task_classes[_CLASS]
    gate = entry.external_acceptance
    assert gate is not None
    assert gate.checker == "lane_brief.py check"
    assert gate.on_refusal == "reject_draft"
    # The slots the contract leaves to the model are exactly the node's drafted slots.
    assert gate.free_text_slots == tuple(slot.value for slot in EnumBriefDraftSlot)
    assert set(gate.shell_parts) >= {
        "ticket_state_acknowledgements",
        "quoted_criteria",
        "constraints_block",
    }
    assert not set(gate.free_text_slots) & set(gate.shell_parts)


def test_external_acceptance_refuses_a_slot_that_is_also_a_shell_part() -> None:
    with pytest.raises(ValidationError, match="both"):
        ModelExternalAcceptance(
            checker="lane_brief.py check",
            on_refusal="reject_draft",
            free_text_slots=("task",),
            shell_parts=("task",),
        )
