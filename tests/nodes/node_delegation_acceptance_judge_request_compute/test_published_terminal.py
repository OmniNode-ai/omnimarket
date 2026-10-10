# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The judge request node decodes the delegate-skill-completed event as it is published.

The in-process ``onex delegate`` port publishes its evidence terminal through
``node_event_emit_effect``, which adds the publish-time envelope fields. The two
fixtures are real payloads read off the dev-202 broker (topic
``onex.evt.omnimarket.delegate-skill-completed.v1``, 2026-10-05, 17:30Z and 17:31Z),
not hand-built dicts. Before this change the node's input model was the producer's
strict response class, which refuses the score keys and the enrichment keys, so the
runtime dead-lettered every one of these events.
"""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)
from omnimarket.nodes.node_delegation_acceptance_judge_request_compute.handlers.handler_delegation_acceptance_judge_request import (
    HandlerDelegationAcceptanceJudgeRequest,
)
from omnimarket.nodes.node_event_emit_effect.enrichment import (
    ENRICHMENT_FIELDS,
    inject_metadata,
)

pytestmark = pytest.mark.unit

NODE = "node_delegation_acceptance_judge_request_compute"
FIXTURES = (
    Path(__file__).parent / "fixtures" / "delegate_skill_completed_in_process.json"
)
PLAIN = "fe11d479-ab01-46bb-b962-42b35d7f54d1"
WITH_LINEAGE = "1333fb52-eb7f-4bf8-944d-1fe81623cc2e"
# What the strict response class refused on the 2026-10-05 dev-202 payload.
PUBLISHED_ONLY_KEYS = {
    "actual_score",
    "required_bar",
    "causation_id",
    "emitted_at",
    "schema_version",
}


def _recorded() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return loaded


def _contract_input_model() -> Any:
    route = _resolve_node_route(NODE)
    model = getattr(import_module(route.input_model_module), route.input_model_name)
    return model


@pytest.mark.parametrize("correlation_id", [PLAIN, WITH_LINEAGE])
def test_contract_input_model_decodes_the_recorded_published_terminal(
    correlation_id: str,
) -> None:
    payload = _recorded()[correlation_id]
    assert set(payload) >= PUBLISHED_ONLY_KEYS

    decoded = _contract_input_model().model_validate(payload)

    assert str(decoded.correlation_id) == correlation_id


@pytest.mark.parametrize("correlation_id", [PLAIN, WITH_LINEAGE])
def test_recorded_terminal_reaches_the_judge_as_a_render_request(
    correlation_id: str,
) -> None:
    payload = _recorded()[correlation_id]
    decoded = _contract_input_model().model_validate(payload)

    request = HandlerDelegationAcceptanceJudgeRequest().handle(decoded)

    assert request.operation is EnumAcceptanceOperation.RENDER
    (item,) = request.items
    assert item.item_id == correlation_id
    assert item.model == payload["model_name"]
    assert item.task_type == payload["task_type"]
    assert item.task_text == payload["prompt_text"]
    assert item.answer_text == payload["response"]
    result = HandlerDelegationAcceptanceJudge().handle(request)
    (batch,) = (b for b in result.batches if b.role == "primary")
    assert payload["prompt_text"] in batch.prompt
    assert payload["model_name"] not in batch.prompt


def test_every_publish_time_enrichment_field_decodes() -> None:
    """The emit effect's whole enrichment set, from its own code, is accepted."""
    bare = {
        key: value
        for key, value in _recorded()[PLAIN].items()
        if key not in set(ENRICHMENT_FIELDS) - {"correlation_id"}
    }
    enriched = inject_metadata(
        bare,
        env={"CLAUDE_CODE_SESSION_ID": "1aee2456-e061-4838-84e7-ab5d241a361f"},
    )
    assert set(ENRICHMENT_FIELDS) <= set(enriched)

    decoded = _contract_input_model().model_validate(enriched)

    assert str(decoded.correlation_id) == PLAIN


def test_wire_response_class_still_refuses_the_published_only_keys() -> None:
    """extra=forbid stays on the producer's response class; only the consumer changed."""
    with pytest.raises(ValidationError) as refused:
        ModelDelegateSkillCompleted.model_validate(_recorded()[PLAIN])

    assert {
        error["loc"][0]
        for error in refused.value.errors()
        if error["type"] == "extra_forbidden"
    } >= PUBLISHED_ONLY_KEYS
