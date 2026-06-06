# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_delegate_skill_orchestrator request/response models."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)


def test_valid_request_minimal() -> None:
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    assert req.prompt == "Write tests for the payment webhook retry path"
    assert req.task_type == "test"
    assert req.source == "claude-code"
    assert req.max_tokens == DELEGATION_DEFAULT_MAX_TOKENS
    assert isinstance(req.correlation_id, UUID)


def test_valid_request_full() -> None:
    req = ModelDelegateSkillRequest(
        prompt="Document the auth flow",
        task_type="document",
        source="codex",
        cwd="/some/path",
        source_file_path="docs/auth.md",
        working_directory="/repo",
        session_id="sess-1",
        recipient="codex",
        codex_sandbox_mode="workspace-write",
        wait=True,
        max_tokens=1200,
        metadata={"repo": "omnimarket", "issue": "OMN-1234"},
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(
            "exactly_two_sentences",
            "max_words_per_sentence_20",
            "plain_text_only",
        ),
    )
    assert req.wait is True
    assert req.cwd == "/some/path"
    assert req.source_file_path == "docs/auth.md"
    assert req.working_directory == "/repo"
    assert req.session_id == "sess-1"
    assert req.recipient == "codex"
    assert req.codex_sandbox_mode == "workspace-write"
    assert req.max_tokens == 1200
    assert req.metadata["repo"] == "omnimarket"
    assert req.quality_contract_mode == "replace_task_class"
    assert req.acceptance_criteria == (
        "exactly_two_sentences",
        "max_words_per_sentence_20",
        "plain_text_only",
    )


def test_invalid_task_type_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Do something",
            task_type="invalid-type",  # type: ignore[arg-type]
            source="claude-code",
        )


def test_empty_prompt_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="",
            task_type="test",
            source="claude-code",
        )


def test_invalid_source_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Test",
            task_type="test",
            source="unknown-adapter",  # type: ignore[arg-type]
        )


def test_non_uuid_correlation_id_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Test",
            task_type="test",
            source="claude-code",
            correlation_id="not-a-uuid",  # type: ignore[arg-type]
        )


def test_unsupported_acceptance_criterion_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Test",
            task_type="test",
            source="claude-code",
            acceptance_criteria=("semantic_magic",),
        )


def test_explicit_max_tokens_hard_limit_accepted() -> None:
    req = ModelDelegateSkillRequest(
        prompt="Use the full local response budget",
        task_type="reasoning",
        source="codex",
        max_tokens=DELEGATION_MAX_TOKENS_HARD_LIMIT,
    )

    assert req.max_tokens == 8192


@pytest.mark.parametrize("max_tokens", [8193, 16384])
def test_max_tokens_above_hard_limit_rejected(max_tokens: int) -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Too much response budget",
            task_type="reasoning",
            source="codex",
            max_tokens=max_tokens,
        )


def test_response_includes_provider_and_metrics() -> None:
    cid = uuid4()
    resp = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=cid,
        task_type="test",
        provider="qwen-coder",
        model_name="Qwen3-Coder-30B",
        prompt_text="Write tests for the webhook",
        quality_gate_passed=True,
        quality_score=0.9,
        metrics=ModelDelegateSkillResponseMetrics(
            cost_usd=0.001,
            latency_ms=2500,
            total_tokens=85,
            tokens_to_compliance=85,
            compliance_attempts=1,
        ),
    )
    assert resp.correlation_id == cid
    assert resp.provider == "qwen-coder"
    assert resp.model_name == "Qwen3-Coder-30B"
    assert resp.prompt_text == "Write tests for the webhook"
    assert resp.quality_gate_passed is True
    assert resp.quality_score == 0.9
    assert resp.metrics.cost_usd == 0.001
    assert resp.metrics.latency_ms == 2500
    assert resp.metrics.total_tokens == 85
    assert resp.metrics.tokens_to_compliance == 85
    assert resp.metrics.compliance_attempts == 1


def test_response_defaults() -> None:
    resp = ModelDelegateSkillResponse(
        status="failed",
        correlation_id=uuid4(),
        task_type="research",
        error_message="boom",
    )
    assert resp.provider == ""
    assert resp.model_name == ""
    assert resp.prompt_text == ""
    assert resp.quality_gate_passed is False
    assert resp.quality_score == 0.0
    assert resp.metrics.cost_usd == 0.0
    assert resp.metrics.total_tokens == 0
    assert resp.metrics.tokens_to_compliance == 0
    assert resp.metrics.compliance_attempts == 0
    assert resp.error_message == "boom"
