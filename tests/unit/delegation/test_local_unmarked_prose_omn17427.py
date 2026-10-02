# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local dispatch preserves a real prose refusal instead of inventing emptiness."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_judge_unavailable_floor_omn13959 import (
    _LOCAL_BACKEND,
    _effect_returning,
)


def test_local_unmarked_prose_names_extraction_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "The queue accepted three jobs. Two completed and one remains pending."
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning(content),
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    captured = []
    evaluate = port._evaluate_quality_gate

    async def record_gate(**kwargs):
        captured.append(kwargs["content"])
        return await evaluate(**kwargs)

    monkeypatch.setattr(port, "_evaluate_quality_gate", record_gate)
    outcome = asyncio.run(
        port._run_single_attempt(
            backend=_LOCAL_BACKEND,
            prompt=f"Summarize: {content}",
            task_type="summarization",
            correlation_id=uuid4(),
            max_tokens=128,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert captured == [content]
    assert outcome.output_refusal is not None
    assert outcome.result.content == ""
    assert not outcome.gate_result.passed
    assert any(
        "ambiguous_unmarked_deliverable" in r
        for r in outcome.gate_result.failure_reasons
    )
    assert all("empty response" not in r for r in outcome.gate_result.failure_reasons)
