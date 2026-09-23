# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dispatch port records the response-contract evidence (OMN-19201).

Measured defect, 2026-09-22: a clean install of the published packages,
delegating to a local OpenAI-compatible model, ran

    onex delegate '<prompt>' --response-contract '<schema>'

and the terminal came back ``status=completed``, one local attempt,
``acceptance_decision=accept``, ``quality_score=1.0``. The CLI then exited 1
with ``completed delegation terminal omits required evidence:
response_contract_evidence`` and wrote none of ``result.txt``, ``receipt.json``
and ``run.json``: the customer's accepted answer was thrown away.

The CLI's refusal is correct -- a caller that asked for a contract is owed the
record of it. The producer half was missing: on the bus path
``handler_inference_intent`` builds the evidence from the payload it sent and
the orchestrator stamps ``validated`` from the gate, but the bus-less local
port, which the customer-local path runs, never put the key on its result.

These tests drive the REAL handler + REAL local dispatch port + REAL quality
gate with only the bifrost backends list patched, and an injected effect that
records the outbound request, so ``conveyed`` is asserted against the system
prompt the effect was actually handed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation.deliverable_extraction import (
    canonical_deliverable_contract_sha256,
    resolve_task_class_deliverable_contract,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
_MODEL_ID = "Qwen3.8-27B"

# The exact contract from the customer-local reproduction.
_FIELDS_CONTRACT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {"type": "array", "items": {"type": "string"}, "minItems": 3}
    },
    "required": ["fields"],
}
_CONFORMING_ANSWER = json.dumps({"fields": ["title", "start_time", "end_time"]})


def _backends() -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": _ENDPOINT,
            "model_name": _MODEL_ID,
            "tier": "local",
            "max_tokens": 8192,
            "timeout_ms": 240000,
            "capabilities": ["agent_delegation", "reasoning"],
        },
    ]


class _RecordingEffect:
    """Injected effect handler that records the outbound request verbatim."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _make_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, content: str
) -> tuple[HandlerDelegateSkill, _RecordingEffect]:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends(),
    )
    effect = _RecordingEffect(content)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), effect


@pytest.mark.unit
async def test_an_accepted_local_answer_carries_the_caller_contract_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE defect: accepted at 1.0, and the terminal carried no evidence."""
    handler, effect = _make_handler(tmp_path, monkeypatch, content=_CONFORMING_ANSWER)
    request = ModelDelegateSkillRequest(
        prompt="List three fields a calendar event needs.",
        task_type="document",
        source="claude-code",
        backend_id="local-coder",
        response_contract=_FIELDS_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    evidence = response.response_contract_evidence
    assert evidence is not None, "local port dropped response_contract_evidence"
    expected = resolve_task_class_deliverable_contract("document", _FIELDS_CONTRACT)
    assert evidence.contract_sha256 == canonical_deliverable_contract_sha256(expected)
    assert evidence.output_shape is EnumDelegationOutputShape.JSON
    assert evidence.channel == "messages[0].content"
    # Conveyed is an observation of the prompt the effect was handed, and the
    # schema is visibly in it.
    assert evidence.conveyed is True
    assert '"minItems": 3' in (effect.calls[0].system_prompt or "")
    # The gate accepted this answer, so the contract was validated.
    assert evidence.validated is True


@pytest.mark.unit
async def test_a_rejected_answer_records_the_contract_as_conveyed_but_not_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``validated`` is the gate's verdict, never a constant."""
    handler, _ = _make_handler(
        tmp_path, monkeypatch, content=json.dumps({"fields": ["only-one"]})
    )
    request = ModelDelegateSkillRequest(
        prompt="List three fields a calendar event needs.",
        task_type="document",
        source="claude-code",
        backend_id="local-coder",
        response_contract=_FIELDS_CONTRACT,
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    evidence = response.response_contract_evidence
    assert evidence is not None
    assert evidence.conveyed is True
    assert evidence.validated is False


@pytest.mark.unit
async def test_no_caller_contract_records_the_task_class_default_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control. Without a caller contract the evidence names the task class's
    own default text contract -- the same thing the bus path records -- and not
    the caller schema, so the evidence tracks the effective contract rather
    than being a constant stamped on every terminal."""
    handler, _ = _make_handler(
        tmp_path, monkeypatch, content="### ANSWER\nTitle, start time, end time."
    )
    request = ModelDelegateSkillRequest(
        prompt="List three fields a calendar event needs.",
        task_type="document",
        source="claude-code",
        backend_id="local-coder",
    )

    response = await handler.handle(request)

    evidence = response.response_contract_evidence
    assert evidence is not None
    assert evidence.output_shape is EnumDelegationOutputShape.MARKDOWN
    assert evidence.contract_sha256 == canonical_deliverable_contract_sha256(
        resolve_task_class_deliverable_contract("document", None)
    )
    assert evidence.contract_sha256 != canonical_deliverable_contract_sha256(
        resolve_task_class_deliverable_contract("document", _FIELDS_CONTRACT)
    )
