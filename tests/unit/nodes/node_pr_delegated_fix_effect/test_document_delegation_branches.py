# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the document adapter with typed terminal responses and real files."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_document_delegation import (
    DocumentDelegationOutcome,
    LiveDocumentDelegation,
    _normalize_completion,
)

pytestmark = pytest.mark.unit


def _response(
    content: str,
    *,
    status: Literal["completed", "failed", "timeout"] = "completed",
    model: str = "test-model",
    attempts: list[ModelDelegateSkillAttemptRecord] | None = None,
    cost: float = 0.0,
) -> ModelDelegateSkillResponse:
    return ModelDelegateSkillResponse(
        status=status,
        correlation_id=uuid4(),
        task_type="document",
        response=content,
        model_name=model,
        attempts=attempts or [],
        metrics=ModelDelegateSkillResponseMetrics(cost_usd=cost),
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("\npass\n\n", "pass\n"),
        ("```python\npass\n```", "pass\n"),
        ("```\npass", "pass\n"),
        ("```", "```\n"),
        ("```python\n```", "\n"),
    ],
)
def test_completion_normalization(content: str, expected: str) -> None:
    assert _normalize_completion(content) == expected


async def test_rewrites_only_existing_files_and_retains_first_routing_identity(
    tmp_path: Path,
) -> None:
    original = '"old"\nvalue = 1\n'
    candidate = '"better"\nvalue = 1\n'
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(original, encoding="utf-8")
    attempt = ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="test-backend",
        model_id="attempt-model",
        quality_gate_passed=True,
    )
    later = attempt.model_copy(update={"tier": "cloud", "backend_id": "later"})
    handle = AsyncMock(
        side_effect=[
            _response(f"```python\n{candidate}```", attempts=[attempt], cost=0.01),
            _response(candidate, model="later-model", attempts=[later], cost=0.02),
        ]
    )
    handler = Mock(handle=handle)
    runner = LiveDocumentDelegation(cast(HandlerDelegateSkill, handler))

    outcome = await runner.run(tmp_path, changed_files=["missing.py", "a.py", "b.py"])

    assert outcome.delegation_model == "test-model"
    assert outcome.cost_usd == pytest.approx(0.03)
    assert outcome.backend_id == "test-backend"
    assert outcome.tier == "local"
    assert outcome.files_rewritten == 2
    assert not (tmp_path / "missing.py").exists()
    for name in ("a.py", "b.py"):
        assert (tmp_path / name).read_text(encoding="utf-8") == candidate
    assert handle.await_count == 2
    request = handle.await_args_list[0].args[0]
    assert request.task_type == "document"
    assert request.source_file_path == str(tmp_path / "a.py")
    assert request.prompt.endswith(original)
    assert request.temperature == 0.0
    assert request.wait is True


@pytest.mark.parametrize(
    ("status", "content"),
    [
        ("failed", '"better"\nvalue = 1\n'),
        ("timeout", '"better"\nvalue = 1\n'),
        ("completed", ""),
        ("completed", '"better"\nvalue = 2\n'),
        ("completed", "invalid python !"),
    ],
)
async def test_refused_or_unusable_response_leaves_source_untouched(
    tmp_path: Path,
    status: Literal["completed", "failed", "timeout"],
    content: str,
) -> None:
    original = '"old"\nvalue = 1\n'
    target = tmp_path / "a.py"
    target.write_text(original, encoding="utf-8")
    handler = Mock(handle=AsyncMock(return_value=_response(content, status=status)))
    outcome = await LiveDocumentDelegation(cast(HandlerDelegateSkill, handler)).run(
        tmp_path, changed_files=["a.py"]
    )
    assert outcome.files_rewritten == 0
    assert outcome.backend_id is None
    assert target.read_text(encoding="utf-8") == original


async def test_attempt_model_and_task_type_fallbacks(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("pass\n", encoding="utf-8")
    attempt = ModelDelegateSkillAttemptRecord(
        tier="cloud",
        backend_id="fallback-backend",
        model_id="attempt-model",
        quality_gate_passed=False,
    )
    handler = Mock(
        handle=AsyncMock(
            return_value=_response("", status="failed", model="", attempts=[attempt])
        )
    )
    runner = LiveDocumentDelegation(cast(HandlerDelegateSkill, handler))
    outcome = await runner.run(tmp_path, changed_files=["a.py"])
    assert outcome.delegation_model == "attempt-model"
    assert outcome.backend_id == "fallback-backend"
    assert outcome.tier == "cloud"
    assert await runner.run(tmp_path, changed_files=[]) == DocumentDelegationOutcome(
        "document", 0.0
    )


def test_live_handler_is_resolved_once_on_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = Mock()
    factory = Mock(return_value=handler)
    monkeypatch.setattr(handler_delegate_skill, "HandlerDelegateSkill", factory)
    runner = LiveDocumentDelegation()
    factory.assert_not_called()
    assert runner._resolve_handler() is handler
    assert runner._resolve_handler() is handler
    factory.assert_called_once_with()
