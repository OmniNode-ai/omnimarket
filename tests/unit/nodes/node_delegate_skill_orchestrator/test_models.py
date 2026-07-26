# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_delegate_skill_orchestrator request/response models."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)

# src/omnimarket -- parents[4] from this test file's directory.
_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "omnimarket"


def test_valid_request_minimal() -> None:
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    assert req.prompt == "Write tests for the payment webhook retry path"
    assert req.task_type == "test"
    assert req.source == "claude-code"
    # OMN-13161: max_tokens is unset by default; the effective value is resolved
    # from the selected backend's per-backend ceiling at dispatch time.
    assert req.max_tokens is None
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


def test_valid_request_external_client_source() -> None:
    """OMN-15158: widen source with a third member for non-adapter callers
    (e.g. the steel/battery delegation client) that are neither the Claude
    Code CLI nor the Codex adapter."""
    req = ModelDelegateSkillRequest(
        prompt="Route a battery match through the delegation node",
        task_type="agent_delegation",
        source="external-client",
    )
    assert req.source == "external-client"


def test_source_literal_has_exactly_three_members() -> None:
    """OMN-15158: pin the widened Literal set so a future edit cannot silently
    narrow it back to two members or grow it beyond the ticketed third."""
    assert get_args(ModelDelegateSkillRequest.model_fields["source"].annotation) == (
        "claude-code",
        "codex",
        "external-client",
    )


def _files_referencing_delegate_skill_models() -> list[Path]:
    """Every src/omnimarket file that imports the request or response model.

    ``model_delegate_skill_request.py`` itself is excluded by the caller --
    the ``source:`` field declaration is not a "reader", it's the field.
    """
    hits: list[Path] = []
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text()
        if "ModelDelegateSkillRequest" in text or "ModelDelegateSkillResponse" in text:
            hits.append(path)
    return hits


def test_no_reader_assumes_two_member_source_set() -> None:
    """OMN-15158 grep-style guard: no code path may read ``.source`` off a
    constructed ``ModelDelegateSkillRequest``/``ModelDelegateSkillResponse``
    instance and branch/compare against the stale two-member set.

    Verified by live grep (2026-07-26) that ``.source`` has zero readers
    across every src/omnimarket file that references either model -- this
    test pins that fact so a future equality/exhaustive-match reader (which
    would silently misroute or misattribute an ``"external-client"``-sourced
    request the same way hostile finding #7 warned about) fails CI instead of
    landing quietly. The wire model's own field declaration is excluded (it
    defines ``source``, it doesn't read it).
    """
    offenders: list[str] = []
    excluded = (
        _SRC_ROOT / "models" / "delegation" / "wire" / "model_delegate_skill_request.py"
    )
    for path in _files_referencing_delegate_skill_models():
        if path == excluded:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "source":
                offenders.append(f"{path.relative_to(_SRC_ROOT)}:{node.lineno}")
    assert not offenders, (
        "Found `.source` attribute access in a file that references "
        "ModelDelegateSkillRequest/ModelDelegateSkillResponse -- this may be "
        "a reader that assumes the stale 2-member Literal set "
        f"(claude-code/codex only): {offenders}"
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


def test_explicit_max_tokens_accepted_above_legacy_hard_limit() -> None:
    """OMN-13161: the request no longer hardcaps at 8192.

    A larger explicit value is accepted on the request; the per-backend ceiling
    (resolved at dispatch time) is what bounds the effective value.
    """
    req = ModelDelegateSkillRequest(
        prompt="Use a large local response budget",
        task_type="reasoning",
        source="codex",
        max_tokens=65536,
    )

    assert req.max_tokens == 65536


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_non_positive_max_tokens_rejected(max_tokens: int) -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Non-positive response budget",
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
    # OMN-14063: no escalation ladder by default — a construction that doesn't
    # pass these fields must not break.
    assert resp.escalation_count == 0
    assert resp.attempts == []


def test_response_carries_escalation_ladder() -> None:
    """OMN-14063: a local->cloud escalation is representable on the typed
    response, carrying WHY the earlier tier was skipped."""
    resp = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=uuid4(),
        task_type="document",
        escalation_count=1,
        attempts=[
            ModelDelegateSkillAttemptRecord(
                tier="local",
                backend_id="local-coder",
                model_id="Qwen3.6-35B-A3B",
                quality_gate_passed=False,
                failure_class="model_unavailable",
                error_message="endpoint http://local.example/health failed health probe",
            ),
            ModelDelegateSkillAttemptRecord(
                tier="cheap_cloud",
                backend_id="cloud-gemini-flash",
                model_id="gemini-2.5-flash-lite",
                quality_gate_passed=True,
                quality_score=1.0,
                cost_usd=0.0018,
            ),
        ],
    )
    assert resp.escalation_count == 1
    assert len(resp.attempts) == 2
    assert resp.attempts[0].failure_class == "model_unavailable"
    assert "failed health probe" in resp.attempts[0].error_message
    assert resp.attempts[1].quality_gate_passed is True
