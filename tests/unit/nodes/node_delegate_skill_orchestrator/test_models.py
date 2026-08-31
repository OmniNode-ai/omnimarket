# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_delegate_skill_orchestrator request/response models."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from omnimarket.models.delegation.wire.model_dispatch_policy import (
    canonical_execution_binding_type,
)
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


def _canonical_binding_type_or_skip() -> type[BaseModel]:
    binding_type = canonical_execution_binding_type()
    if binding_type is None:
        pytest.skip("requires Core ModelDelegationExecutionBinding")
    return binding_type


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


def test_backend_id_defaults_to_none() -> None:
    """OMN-15180: omitting backend_id preserves pre-existing behavior — None,
    not a default pin, so unpinned callers are unaffected by this field's
    addition."""
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    assert req.backend_id is None


def test_backend_id_round_trips_through_model_dump_and_validate() -> None:
    """OMN-15180: an explicit backend_id pin survives a full serialize/parse
    round-trip — the exact shape a wire-level caller (e.g. steel's
    LlmBusDelegationClient) sends and the shape the orchestrator's own
    contract-validation/CLI compile path parses back."""
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="code_generation",
        source="claude-code",
        backend_id="local-coder-mlx",
    )
    assert req.backend_id == "local-coder-mlx"

    dumped = req.model_dump(mode="json")
    assert dumped["backend_id"] == "local-coder-mlx"

    round_tripped = ModelDelegateSkillRequest.model_validate(dumped)
    assert round_tripped.backend_id == "local-coder-mlx"
    assert round_tripped == req


def test_backend_id_widening_preserves_frozen_extra_forbid() -> None:
    """OMN-15180: adding backend_id is additive-only — the model stays frozen
    and still rejects unknown fields (extra='forbid' is not silently loosened)."""
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    with pytest.raises(ValidationError):
        req.backend_id = "should-not-be-settable"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Write tests for the payment webhook retry path",
            task_type="test",
            source="claude-code",
            unknown_field="not allowed",  # type: ignore[call-arg]
        )


def test_backend_pinned_single_attempt_policy_round_trips_canonically() -> None:
    request = ModelDelegateSkillRequest(
        prompt="Inspect this constrained lab task.",
        task_type="research",
        source="codex",
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="c" * 64,
    )

    dumped = request.model_dump(mode="json")

    assert dumped["dispatch_policy"] == "backend-pinned-single-attempt.v1"
    assert dumped["rendered_contract_sha256"] == "c" * 64
    assert ModelDelegateSkillRequest.model_validate(dumped) == request


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"tenant_id": None, "backend_id": "local-coder-mlx"},
            "verified non-empty tenant_id",
            id="missing-tenant",
        ),
        pytest.param(
            {"tenant_id": "rsd-lab", "backend_id": None},
            "non-empty backend_id",
            id="missing-backend",
        ),
        pytest.param(
            {"tenant_id": " rsd-lab", "backend_id": "local-coder-mlx"},
            "verified non-empty tenant_id",
            id="padded-tenant",
        ),
        pytest.param(
            {"tenant_id": "rsd-lab", "backend_id": " local-coder-mlx"},
            "non-empty backend_id",
            id="padded-backend",
        ),
    ],
)
def test_backend_pinned_single_attempt_requires_explicit_verified_binding(
    overrides: dict[str, str | None], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDelegateSkillRequest(
            prompt="Inspect this constrained lab task.",
            task_type="research",
            source="codex",
            dispatch_policy="backend-pinned-single-attempt.v1",
            **overrides,
        )


@pytest.mark.parametrize(
    ("rendered_contract_sha256", "message"),
    [
        pytest.param(None, "rendered_contract_sha256", id="missing-digest"),
        pytest.param("A" * 64, "String should match pattern", id="uppercase-digest"),
        pytest.param("a" * 63, "String should match pattern", id="short-digest"),
    ],
)
def test_backend_pinned_single_attempt_requires_caller_rendered_contract_digest(
    rendered_contract_sha256: str | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDelegateSkillRequest(
            prompt="Inspect this constrained lab task.",
            task_type="research",
            source="codex",
            tenant_id="rsd-lab",
            backend_id="local-coder-mlx",
            dispatch_policy="backend-pinned-single-attempt.v1",
            rendered_contract_sha256=rendered_contract_sha256,
        )


def test_rendered_contract_digest_requires_explicit_dispatch_policy() -> None:
    with pytest.raises(ValidationError, match="requires an explicit dispatch_policy"):
        ModelDelegateSkillRequest(
            prompt="Inspect this constrained lab task.",
            task_type="research",
            source="codex",
            rendered_contract_sha256="a" * 64,
        )


def test_execution_binding_round_trips_with_canonical_single_attempt_claim() -> None:
    correlation_id = uuid4()
    binding_type = _canonical_binding_type_or_skip()
    response = ModelDelegateSkillResponse(
        status="failed",
        correlation_id=correlation_id,
        task_type="research",
        tenant_id="rsd-lab",
        model_name="Qwen3.6-35B-A3B",
        attempts_count=1,
        attempts=[
            ModelDelegateSkillAttemptRecord(
                tier="local",
                backend_id="local-coder-mlx",
                model_id="Qwen3.6-35B-A3B",
                quality_gate_passed=False,
                failure_class="timeout",
                error_message="adapter timeout",
            )
        ],
        execution_binding=binding_type(
            correlation_id=correlation_id,
            tenant_id="rsd-lab",
            backend_id="local-coder-mlx",
            served_model_id="Qwen3.6-35B-A3B",
            rendered_contract_sha256="a" * 64,
            attempt_count=1,
            fallback_used=False,
            judge_used=False,
            dispatch_policy="backend-pinned-single-attempt.v1",
            terminal_kind="failed",
        ),
    )

    dumped = response.model_dump(mode="json")

    assert dumped["execution_binding"] == {
        "correlation_id": str(correlation_id),
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "served_model_id": "Qwen3.6-35B-A3B",
        "rendered_contract_sha256": "a" * 64,
        "attempt_count": 1,
        "fallback_used": False,
        "judge_used": False,
        "dispatch_policy": "backend-pinned-single-attempt.v1",
        "terminal_kind": "failed",
    }
    assert ModelDelegateSkillResponse.model_validate(dumped) == response


@pytest.mark.parametrize(
    ("response_overrides", "binding_overrides", "message"),
    [
        pytest.param(
            {"status": "completed"},
            {},
            "terminal_kind must match",
            id="terminal-kind-mismatch",
        ),
        pytest.param(
            {"attempts_count": 2},
            {},
            "requires attempts_count=1",
            id="attempt-count-mismatch",
        ),
        pytest.param(
            {},
            {"fallback_used": True},
            "Input should be False",
            id="fallback-used-mismatch",
        ),
        pytest.param(
            {},
            {"backend_id": "alternate-backend"},
            "backend_id must match the recorded attempt",
            id="alternate-backend-mismatch",
        ),
    ],
)
def test_execution_binding_rejects_terminal_mismatches(
    response_overrides: dict[str, object],
    binding_overrides: dict[str, object],
    message: str,
) -> None:
    _canonical_binding_type_or_skip()
    correlation_id = uuid4()
    binding = {
        "correlation_id": correlation_id,
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "served_model_id": "Qwen3.6-35B-A3B",
        "rendered_contract_sha256": "b" * 64,
        "attempt_count": 1,
        "fallback_used": False,
        "judge_used": False,
        "dispatch_policy": "backend-pinned-single-attempt.v1",
        "terminal_kind": "failed",
    }
    binding.update(binding_overrides)
    response = {
        "status": "failed",
        "correlation_id": correlation_id,
        "task_type": "research",
        "tenant_id": "rsd-lab",
        "model_name": "Qwen3.6-35B-A3B",
        "attempts_count": 1,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-coder-mlx",
                "model_id": "Qwen3.6-35B-A3B",
                "quality_gate_passed": False,
            }
        ],
        "execution_binding": binding,
    }
    response.update(response_overrides)

    with pytest.raises(ValidationError, match=message):
        ModelDelegateSkillResponse.model_validate(response)


def test_response_contract_defaults_to_none() -> None:
    """OMN-15193: omitting response_contract preserves pre-existing behavior --
    None, not an implicit schema, so unpinned callers are unaffected."""
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    assert req.response_contract is None


def test_response_contract_round_trips_through_model_dump_and_validate() -> None:
    """OMN-15193: a declared JSON-Schema response contract survives a full
    serialize/parse round-trip -- the exact shape a wire-level caller (e.g.
    steel's LlmBusDelegationClient) sends and the shape the orchestrator's own
    contract-validation/CLI compile path parses back."""
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["action", "confidence"],
    }
    req = ModelDelegateSkillRequest(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        source="claude-code",
        response_contract=schema,
    )
    assert req.response_contract == schema

    dumped = req.model_dump(mode="json")
    assert dumped["response_contract"] == schema

    round_tripped = ModelDelegateSkillRequest.model_validate(dumped)
    assert round_tripped.response_contract == schema
    assert round_tripped == req


def test_response_contract_widening_preserves_frozen_extra_forbid() -> None:
    """OMN-15193: adding response_contract is additive-only -- the model stays
    frozen and still rejects unknown fields (extra='forbid' is not silently
    loosened)."""
    req = ModelDelegateSkillRequest(
        prompt="Write tests for the payment webhook retry path",
        task_type="test",
        source="claude-code",
    )
    with pytest.raises(ValidationError):
        req.response_contract = {"type": "object"}  # type: ignore[misc]

    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(
            prompt="Write tests for the payment webhook retry path",
            task_type="test",
            source="claude-code",
            unknown_field="not allowed",  # type: ignore[call-arg]
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
    assert resp.attempts_count == 1
    assert resp.attempts == []


def test_response_carries_escalation_ladder() -> None:
    """OMN-14063: a local->cloud escalation is representable on the typed
    response, carrying WHY the earlier tier was skipped."""
    resp = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=uuid4(),
        task_type="document",
        escalation_count=1,
        attempts_count=2,
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
    assert resp.attempts_count == 2
    assert len(resp.attempts) == 2
    assert resp.attempts[0].failure_class == "model_unavailable"
    assert "failed health probe" in resp.attempts[0].error_message
    assert resp.attempts[1].quality_gate_passed is True
