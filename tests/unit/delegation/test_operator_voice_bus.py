# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator-voice veto, decided through the gate's real bus wiring.

The gate node is discovered from its own contract, wired with
``wire_from_manifest`` onto a real message dispatch engine and driven by a
command published on its declared request topic. The verdict is read from its
declared result topic, so what is asserted is what a consumer of the bus sees.
The bus is the in-memory one; the integration test of the same name in
``tests/integration/node_delegation_quality_gate_reducer`` runs the same
exchange on the Redpanda test lane.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)
from tests.unit.delegation.test_omn17445_effect_and_gate_failure_terminals import (
    _GATE_CONTRACT,
    _GATE_REQUEST_TOPIC,
    _GATE_RESULT_TOPIC,
    _run_one_record,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "operator_voice"
_EVENT_TYPE = "omnibase-infra.delegation-quality-gate-request"


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _wire(draft: str, source: str, correlation_id: object) -> dict[str, object]:
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", source)
    return {
        "intent": "quality_gate",
        "payload": {
            "correlation_id": str(correlation_id),
            "task_type": "summarization",
            "llm_response_content": draft,
            "dod_deterministic": list(deterministic),
            "dod_heuristic": list(heuristic),
            "grounding_source": source,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft_file", "refused"),
    [
        ("draft_invented_approval_a.txt", True),
        ("draft_invented_approval_b.txt", True),
        ("draft_plain_summary.txt", False),
    ],
)
async def test_operator_voice_decision_on_the_in_memory_bus(
    draft_file: str, refused: bool
) -> None:
    correlation_id = uuid4()
    envelope = await _run_one_record(
        contract_path=_GATE_CONTRACT,
        command_topic=_GATE_REQUEST_TOPIC,
        terminal_topic=_GATE_RESULT_TOPIC,
        payload=_wire(_text(draft_file), _text("source_no_grant.txt"), correlation_id),
        event_type=_EVENT_TYPE,
        correlation_id=correlation_id,
    )

    assert envelope is not None, "the gate published no verdict on its result topic"
    verdict = envelope.payload
    assert isinstance(verdict, dict)
    reasons = [str(r) for r in verdict["failure_reasons"]]
    assert verdict["passed"] is (not refused), reasons
    assert any(r.startswith("OPERATOR_VOICE") for r in reasons) is refused, reasons
