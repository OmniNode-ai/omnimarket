# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21344: a task class's delegation time limit comes from the contract or a site overlay.

Large delegations failed on a fixed 240 s cap: every class's ceiling was
validated against ``le=240`` in code, so no contract or overlay could raise
one. The contract's ``execution_budgets`` entry is now the base, a site
overlay at ``<root>/delegation_budgets/overlay.yaml`` may override it per
class, the bound is 900 s, and the runtime port's wait and the handler's own
bound follow the largest declared class.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.execution_budget_overlay import (
    EXECUTION_BUDGET_OVERLAY_ID,
    largest_execution_budget,
    resolve_execution_budget,
)
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV, NodeOverlayError
from omnimarket.inference.task_class_authority import (
    ModelTaskClassExecutionBudget,
    load_task_class_authority,
    resolve_task_class_execution_budget,
)
from omnimarket.models.delegation.wire import model_delegate_skill_response
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    load_handler_execution_budget,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    load_runtime_delegation_dispatch_config,
)

pytestmark = pytest.mark.unit

TASK_CLASS = "code_generation"
OVERLAY_CEILING = 600
STUB_CALL_SECONDS = 300


def _write_overlay(root: Path, budgets: dict[str, Any]) -> Path:
    path = root / EXECUTION_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"execution_budgets": budgets}), encoding="utf-8")
    return path


@pytest.fixture
def overlay_600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _write_overlay(
        tmp_path,
        {TASK_CLASS: {"task_class_timeout_ceiling_seconds": OVERLAY_CEILING}},
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    return path


@pytest.fixture
def no_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "empty"))


class _StubCallPort:
    """A provider call that reports it ran 300 s; records what the handler gave it."""

    def __init__(self) -> None:
        self.execution_timeout_seconds: int | None = None

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.execution_timeout_seconds = kwargs["execution_timeout_seconds"]
        await asyncio.sleep(0)
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


@pytest.mark.asyncio
async def test_an_overlay_limit_of_600s_lets_a_300s_call_complete(
    overlay_600: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 falsifier. At the merge base the 300 s request is refused at 240 s."""
    waits: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(aw: Any, timeout: float | None) -> Any:
        waits.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(handler_module.asyncio, "wait_for", recording_wait_for)
    port = _StubCallPort()
    handler = HandlerDelegateSkill(dispatch_port=cast(Any, port))

    terminal = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="write a test",
            task_type=TASK_CLASS,
            source="claude-code",
            correlation_id=uuid4(),
            requested_timeout_seconds=STUB_CALL_SECONDS,
        )
    )

    assert isinstance(terminal, ModelDelegateSkillCompleted), terminal.error_message
    assert terminal.budget_refusal is None
    assert port.execution_timeout_seconds == STUB_CALL_SECONDS
    assert terminal.budget_evidence is not None
    assert terminal.budget_evidence.task_class_timeout_ceiling_seconds == (
        OVERLAY_CEILING
    )
    assert waits
    assert waits[0] is not None
    assert waits[0] >= STUB_CALL_SECONDS


def test_the_budget_names_the_overlay_it_came_from(overlay_600: Path) -> None:
    resolved = resolve_execution_budget(TASK_CLASS)

    assert resolved.budget.task_class_timeout_ceiling_seconds == OVERLAY_CEILING
    assert resolved.source == f"overlay:{overlay_600}#execution_budgets.{TASK_CLASS}"
    # Fields the overlay leaves unset keep the contract's values.
    assert resolved.budget.terminal_delivery_margin_seconds == 60


def test_without_an_overlay_the_budget_names_the_contract(no_overlay: None) -> None:
    resolved = resolve_execution_budget(TASK_CLASS)

    assert resolved.source == (
        f"contract:task_class_contracts.v1.yaml#execution_budgets.{TASK_CLASS}"
    )
    assert resolved.budget == resolve_task_class_execution_budget(TASK_CLASS)


def test_the_port_wait_and_handler_bound_follow_the_largest_class(
    overlay_600: Path,
) -> None:
    largest = largest_execution_budget()
    assert largest is not None
    assert largest.budget.task_class_timeout_ceiling_seconds == OVERLAY_CEILING

    wait = load_runtime_delegation_dispatch_config().wait_timeout_seconds
    bound = load_handler_execution_budget().max_handler_duration_seconds

    assert wait == OVERLAY_CEILING + 60
    assert bound == OVERLAY_CEILING
    assert bound < wait


def test_without_an_overlay_the_contract_values_stand(no_overlay: None) -> None:
    assert load_runtime_delegation_dispatch_config().wait_timeout_seconds == 300
    assert load_handler_execution_budget().max_handler_duration_seconds == 240


def test_a_ceiling_above_900s_is_refused() -> None:
    ModelTaskClassExecutionBudget(
        task_class_timeout_ceiling_seconds=900, terminal_delivery_margin_seconds=60
    )
    with pytest.raises(ValidationError, match=r"less_than_equal|less than or equal"):
        ModelTaskClassExecutionBudget(
            task_class_timeout_ceiling_seconds=901,
            terminal_delivery_margin_seconds=60,
        )


@pytest.mark.parametrize(
    ("budgets", "match"),
    [
        (
            {"no_such_class": {"task_class_timeout_ceiling_seconds": 300}},
            "no_such_class",
        ),
        ({TASK_CLASS: {"task_class_timeout_ceiling_seconds": 901}}, "invalid budget"),
        ({TASK_CLASS: {"task_class_timeout_ceiling_seconds": 30}}, "invalid budget"),
        ({TASK_CLASS: {"timeout": 300}}, "invalid"),
    ],
)
def test_a_broken_overlay_is_refused_not_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budgets: dict[str, Any],
    match: str,
) -> None:
    _write_overlay(tmp_path, budgets)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))

    with pytest.raises(NodeOverlayError, match=match):
        resolve_execution_budget(TASK_CLASS)


def test_a_terminal_carrying_budget_source_decodes() -> None:
    """Consumer first: the key is accepted before any producer stamps it."""
    assert "budget_source" in model_delegate_skill_response._FORTHCOMING_TERMINAL_KEYS
    terminal = ModelDelegateSkillCompleted.model_validate(
        {
            "status": "completed",
            "correlation_id": str(uuid4()),
            "task_type": TASK_CLASS,
            "budget_source": "overlay:/x/delegation_budgets/overlay.yaml",
        }
    )
    assert "budget_source" not in terminal.model_dump(mode="json")


def test_the_authority_the_delegate_cli_reads_answers_with_the_overlay(
    overlay_600: Path,
) -> None:
    """The onex delegate CLI asks the registered authority, so it sees the overlay too."""
    budget = load_task_class_authority().execution_budget(TASK_CLASS)

    assert budget.task_class_timeout_ceiling_seconds == OVERLAY_CEILING
