# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20164: neither acceptance path performs model judging.

The requested slice-40 task 40.5 fixture set is absent from this checkout.
The refusal controls below cover available deterministic checks; the missing
fixture inventory must be reported as an AC2 gap in the PR.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelQualityGateInput,
    ModelQualityGateIntent,
)

from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class RecordingJudge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def score(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            verdict=EnumDelegationJudgeVerdict.PASS, actual_score=1.0, failure_kind=None
        )


async def decide(
    path: str, task_type: str, content: str, tmp_path: Path, judge: RecordingJudge
) -> ModelQualityGateResult:
    if path == "bus":
        handler = HandlerQualityGateIntent()
        handler._judge = judge
        deterministic, heuristic = resolve_task_class_dod_checks(task_type)
        intent = ModelQualityGateIntent(
            payload=ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type=task_type,
                llm_response_content=content,
                dod_deterministic=deterministic,
                dod_heuristic=heuristic,
            )
        )
        output = await handler.handle_async(intent)
        assert len(output.events) == 1, "the gate emits only its deterministic result"
        assert output.events[0] == handler.handle(intent)
        return cast(ModelQualityGateResult, output.events[0])
    port = LocalDelegationDispatchPort(evidence_db_path=tmp_path / "evidence.sqlite")
    port._judge = judge
    return await port._evaluate_quality_gate(
        correlation_id=uuid4(),
        task_type=task_type,
        prompt="Implement addition.",
        content=content,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
    )


@pytest.mark.parametrize("path", ["bus", "local"])
@pytest.mark.parametrize(
    "task_type",
    ["code_generation", "test", "validator_generation", "refactor", "research"],
)
async def test_no_judge_call_in_gate(path: str, task_type: str, tmp_path: Path) -> None:
    judge = RecordingJudge()
    await decide(
        path,
        task_type,
        "def add(a: int, b: int) -> int:\n    return a + b",
        tmp_path,
        judge,
    )
    assert judge.calls == []


@pytest.mark.parametrize("path", ["bus", "local"])
@pytest.mark.parametrize(
    "content", ["", "I cannot help with that request.", "def add(:\n    return 1"]
)
async def test_vetoed_fixtures_refused_deterministic_controls(
    path: str, content: str, tmp_path: Path
) -> None:
    judge = RecordingJudge()
    result = await decide(path, "code_generation", content, tmp_path, judge)
    assert result.passed is False
    assert result.failure_reasons
    assert judge.calls == []


@pytest.mark.parametrize("path", ["bus", "local"])
async def test_vetoed_fixtures_refused_known_gap_stub(
    path: str, tmp_path: Path
) -> None:
    """Known gap: propose an AST check rejecting pass-only function bodies.

    This pins the unrefused control so AC2 cannot be reported as fully proved.
    """
    judge = RecordingJudge()
    result = await decide(
        path, "code_generation", "def add(a, b):\n    pass", tmp_path, judge
    )
    assert result.passed is True
    assert judge.calls == []
