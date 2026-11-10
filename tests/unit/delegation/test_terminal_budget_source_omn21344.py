# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21344 AC2: the delegate-skill terminal names where its budget came from.

omnimarket#4601 let a site overlay raise a task class's execution budget and
taught the terminal to accept a ``budget_source`` key without stamping one.
omnibase_infra#5470 copies that key into receipt.json. This is the producer
half: the handler stamps the source of the budget the run was held to, the
contract entry or the overlay file, on every terminal it builds, and the wire
payload carries it. At the merge base the field is undeclared, so the terminal
has no ``budget_source`` and each test here fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.execution_budget_overlay import EXECUTION_BUDGET_OVERLAY_ID
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

pytestmark = pytest.mark.unit

TASK_CLASS = "code_generation"
CONTRACT_SOURCE = (
    f"contract:task_class_contracts.v1.yaml#execution_budgets.{TASK_CLASS}"
)


class _StubPort:
    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        await asyncio.sleep(0)
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


def _request(timeout_seconds: int | None) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="write a test",
        task_type=TASK_CLASS,
        source="claude-code",
        correlation_id=uuid4(),
        requested_timeout_seconds=timeout_seconds,
    )


def _handler() -> HandlerDelegateSkill:
    return HandlerDelegateSkill(dispatch_port=cast(Any, _StubPort()))


@pytest.fixture
def overlay_600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / EXECUTION_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "execution_budgets": {
                    TASK_CLASS: {"task_class_timeout_ceiling_seconds": 600}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    return path


@pytest.fixture
def no_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "empty"))


@pytest.mark.asyncio
async def test_a_run_under_an_overlay_names_the_overlay_on_its_terminal(
    overlay_600: Path,
) -> None:
    terminal = await _handler().handle(_request(300))

    assert isinstance(terminal, ModelDelegateSkillCompleted), terminal.error_message
    assert terminal.budget_evidence is not None
    assert terminal.budget_evidence.task_class_timeout_ceiling_seconds == 600
    expected = f"overlay:{overlay_600}#execution_budgets.{TASK_CLASS}"
    assert terminal.budget_source == expected
    assert terminal.model_dump(mode="json")["budget_source"] == expected


@pytest.mark.asyncio
async def test_a_run_without_an_overlay_names_the_contract(no_overlay: None) -> None:
    terminal = await _handler().handle(_request(None))

    assert isinstance(terminal, ModelDelegateSkillCompleted), terminal.error_message
    assert terminal.budget_evidence is not None
    assert terminal.budget_evidence.task_class_timeout_ceiling_seconds == 240
    assert terminal.budget_source == CONTRACT_SOURCE
    assert terminal.model_dump(mode="json")["budget_source"] == CONTRACT_SOURCE


@pytest.mark.asyncio
async def test_a_pre_dispatch_refusal_names_the_budget_it_was_refused_under(
    no_overlay: None,
) -> None:
    terminal = await _handler().handle(_request(300))

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.budget_refusal is not None
    assert terminal.budget_refusal.task_class_timeout_ceiling_seconds == 240
    assert terminal.budget_source == CONTRACT_SOURCE
    assert terminal.model_dump(mode="json")["budget_source"] == CONTRACT_SOURCE
