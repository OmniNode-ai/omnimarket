# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegate-skill response model."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, Self
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
    ModelPremiumCounterfactual,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)


class ModelDelegateSkillAttemptRecord(BaseModel):
    """One tier/backend attempt in a delegation's escalation ladder (OMN-14063).

    Populated for the bus-less local dispatch path from the per-attempt list
    ``LocalDelegationDispatchPort.dispatch`` already builds internally; prior to
    OMN-14063 that list was computed but never threaded onto the typed response,
    so a local->cloud escalation (e.g. triggered by a flaky health probe) was
    invisible to the caller — visible only by grepping the capture-file log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str = Field(...)
    backend_id: str = Field(...)
    model_id: str = Field(...)
    quality_gate_passed: bool = Field(...)
    quality_score: float | None = Field(default=None)
    cost_usd: float = Field(default=0.0, ge=0.0)
    failure_class: str | None = Field(
        default=None,
        description="Transport failure_class (e.g. 'model_unavailable') when this "
        "attempt was skipped/failed before inference ran; None for a quality-gate "
        "verdict or a successful attempt.",
    )
    error_message: str = Field(
        default="",
        description="Why this tier was skipped/failed, e.g. 'endpoint <url> failed "
        "health probe' — the same reason previously visible only in the capture log.",
    )
    # OMN-16932: the accept/climb verdict for this rung, carried onto the
    # CONSUMER-facing terminal rather than left in orchestrator-internal state.
    # The ticket exists because an escalation past a working free rung was
    # invisible here: a reader could see that a later rung ran and had to infer
    # why the earlier one was abandoned. ``None`` means this attempt never
    # reached an accept/climb decision (a transport skip on the bus-less path),
    # which is a different fact from "it was rejected" and is typed as such.
    acceptance_decision: EnumDelegationAcceptanceDecision | None = Field(
        default=None,
        description="Whether this rung's response was accepted or the ladder climbed past it.",
    )
    acceptance_reason: EnumDelegationAcceptanceReason | None = Field(
        default=None,
        description="Typed reason for the accept/climb decision on this rung.",
    )


class ModelDelegateSkillResponseMetrics(BaseModel):
    """Cost and latency metrics for a delegation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tokens_to_compliance: int = Field(default=0, ge=0)
    compliance_attempts: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    cost_savings_usd: float = Field(default=0.0, ge=0.0)
    frontier_costs_usd: dict[str, float] = Field(default_factory=dict)
    premium_counterfactual: ModelPremiumCounterfactual | None = Field(
        default=None,
        description=(
            "Pinned premium counterfactual {model, price, as_of, tokens, cost} "
            "(OMN-13355). cost_savings_usd = counterfactual_cost_usd - cost_usd."
        ),
    )
    latency_ms: int = Field(default=0, ge=0)


class ModelDelegateSkillResponse(BaseModel):
    """Typed delegation result returned to requesting adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "failed", "timeout"] = Field(...)
    correlation_id: UUID = Field(...)
    task_type: str = Field(...)
    # string-id-ok: tenant_id is a named tenant identifier (slug), not a UUID.
    # OMN-14485: the terminal event `delegate-skill-completed.v1` is auto-published
    # from this response, and node_projection_delegation reads the row's tenant
    # from that terminal. Before this field the response could not carry the
    # request-resolved tenant, so the terminal was tenant-less and every row fell
    # back to the 'omninode' column default -- a LIVE NO-OP for tenant-carry on the
    # merged multitenant write-path (OMN-14208 epic). None means no tenant was
    # resolved (request tenant_id absent AND ONEX_TENANT_ID unset); the projection
    # then applies the column default. The verified value is resolved upstream and
    # via the ONEX_TENANT_ID interim (OMN-14058), never self-reported here.
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Multi-tenant isolation identifier carried onto the terminal event so "
            "the delegation_events projection row stamps a real tenant. None means "
            "the 'omninode' column default applies."
        ),
    )
    provider: str = Field(default="")
    model_name: str = Field(default="")
    model_cloud_baseline: str = Field(default="")
    pricing_manifest_version: int = Field(default=0, ge=0)
    prompt_text: str = Field(default="")
    response: str = Field(default="")
    quality_gate_passed: bool = Field(default=False)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    required_quality_bar: float | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        ge=0.0,
        le=1.0,
        description="Authoritative minimum quality score applied to this result.",
    )
    score_vs_required_bar: EnumQualityScoreComparison | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Typed comparison between quality_score and its required bar.",
    )
    failed_acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description="Authoritative quality-gate criteria that rejected this result.",
    )
    terminal_failure_cause: EnumDelegationTerminalFailureCause | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Stable machine-readable terminal failure cause, when known.",
    )
    quality_gates_failed: list[str] = Field(default_factory=list)
    metrics: ModelDelegateSkillResponseMetrics = Field(
        default_factory=ModelDelegateSkillResponseMetrics,
    )
    error_message: str = Field(default="")
    escalation_count: int = Field(
        default=0,
        ge=0,
        description="Number of up-tier escalations before the terminal attempt "
        "(OMN-14063). 0 means the first-resolved tier answered directly.",
    )
    attempts_count: int = Field(
        default=1,
        ge=1,
        description="Authoritative total inference calls including compliance "
        "repairs, same-tier retries, and the terminal attempt.",
    )
    attempts: list[ModelDelegateSkillAttemptRecord] = Field(
        default_factory=list,
        description="Best available per-attempt detail in order. Rich local "
        "attempts include the terminal attempt; escalation_history fallback may "
        "contain rejected attempts only. attempts_count remains authoritative.",
    )

    @model_validator(mode="after")
    def validate_structured_terminal_evidence(self) -> Self:
        """Preserve the canonical Core terminal-evidence invariants.

        This mirrors ``ModelDelegationResult.validate_structured_terminal_evidence``
        in omnibase_core 0.46.8 clause for clause (OMN-15539 / OMN-15464). The two
        models are the two ends of one delegation terminal, so any verdict Core
        refuses to construct must also be un-constructable here — otherwise a
        producer that bypasses the Core model (the bus-less local dispatch port, a
        direct response construction, a future adapter) publishes a contradiction
        that the canonical wire DTO would have blocked. The agreement is pinned by
        ``tests/unit/delegation/test_seam_quality_gate_semantics_omn15539.py``,
        which drives BOTH models from one fixture; keep the two validators in
        step or that seam test fails.
        """
        if any(not item.strip() for item in self.failed_acceptance_criteria):
            msg = "failed_acceptance_criteria entries must not be blank"
            raise ValueError(msg)

        required_bar = self.required_quality_bar
        comparison = self.score_vs_required_bar
        if (required_bar is None) != (comparison is None):
            msg = (
                "required_quality_bar and score_vs_required_bar must be "
                "provided together"
            )
            raise ValueError(msg)

        if required_bar is not None and comparison is not None:
            expected = (
                EnumQualityScoreComparison.BELOW_BAR
                if self.quality_score < required_bar
                else EnumQualityScoreComparison.AT_OR_ABOVE_BAR
            )
            if comparison is not expected:
                msg = (
                    "score_vs_required_bar must match quality_score and "
                    "required_quality_bar"
                )
                raise ValueError(msg)

            if (
                comparison is EnumQualityScoreComparison.BELOW_BAR
                and self.quality_gate_passed
            ):
                msg = (
                    "quality_gate_passed response cannot be below required_quality_bar"
                )
                raise ValueError(msg)

            if (
                comparison is EnumQualityScoreComparison.AT_OR_ABOVE_BAR
                and not self.quality_gate_passed
                and not self.failed_acceptance_criteria
            ):
                msg = (
                    "quality-failed response at or above required_quality_bar must "
                    "carry failed_acceptance_criteria"
                )
                raise ValueError(msg)

        if self.quality_gate_passed and self.failed_acceptance_criteria:
            msg = "quality_gate_passed response cannot carry failed_acceptance_criteria"
            raise ValueError(msg)
        if self.quality_gate_passed and self.terminal_failure_cause is not None:
            msg = "successful delegation cannot carry terminal_failure_cause"
            raise ValueError(msg)
        return self


# HTTP 429 / RESOURCE_EXHAUSTED as providers phrase it. Matched only as a
# FALLBACK, after the typed ``failure_class`` on the attempt ladder: the string
# match exists because the bus dispatch port reports the provider's raw error
# text without classifying it, and a quota refusal that reaches the terminal
# unclassified is exactly the case this ticket exists to stop mislabelling.
_QUOTA_ERROR_PATTERN = re.compile(
    r"\b429\b|resource_exhausted|quota exceeded|quota_exceeded|rate limit exceeded",
    re.IGNORECASE,
)


def resolve_terminal_failure_cause(
    attempts: Sequence[ModelDelegateSkillAttemptRecord],
    *,
    error_message: str = "",
) -> EnumDelegationTerminalFailureCause | None:
    """Classify a delegation's terminal failure cause from its attempt ladder.

    Typed evidence first: an attempt whose ``failure_class`` already equals a
    known enum value is authoritative. Only when no attempt is classified does
    this fall back to matching the provider's raw error text, so a port that
    learns to classify its own failures immediately takes precedence over the
    regex without any change here.

    Returns ``None`` when nothing in the ladder or the outer error names a
    cause the enum can express — an unclassified failure stays unclassified
    rather than being coerced into the nearest member.
    """
    known = {member.value for member in EnumDelegationTerminalFailureCause}
    for attempt in attempts:
        raw = (attempt.failure_class or "").strip().lower()
        if raw in known:
            return EnumDelegationTerminalFailureCause(raw)
    texts = [attempt.error_message for attempt in attempts]
    texts.append(error_message)
    if any(text and _QUOTA_ERROR_PATTERN.search(text) for text in texts):
        return EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    return None


def _authoritative_attempt_ladder_verdict(
    response: ModelDelegateSkillResponse,
) -> bool | None:
    """Return an attempt-ladder success verdict only when the ladder is complete."""
    if not response.attempts:
        return None
    if response.attempts_count > len(response.attempts):
        return None
    return any(attempt.quality_gate_passed for attempt in response.attempts)


def _evidence_indicates_success(response: ModelDelegateSkillResponse) -> bool:
    """Status-independent success evidence for validating failed terminals."""
    if response.terminal_failure_cause is not None:
        return False
    attempt_verdict = _authoritative_attempt_ladder_verdict(response)
    if attempt_verdict is not None:
        return attempt_verdict
    return response.quality_gate_passed


def delegate_skill_succeeded(response: ModelDelegateSkillResponse) -> bool:
    """Resolve the COMPOSITE verdict for a delegation from its own evidence.

    The dispatch port's ``status`` is one input, not the answer. Live on
    2026-07-29 a command whose every ladder attempt was refused with HTTP 429
    still reported ``status="completed"`` / ``quality_gate_passed=true``; the
    honest verdict was already present on the same payload, in the attempt
    ladder, and nothing consulted it.

    A delegation succeeded only when ALL of these hold:

    * the port reported ``completed`` (a ``failed``/``timeout`` port verdict is
      never upgraded here — this function can only ever make a verdict worse);
    * no typed terminal failure cause was classified;
    * the attempt ladder, WHEN AUTHORITATIVE, contains at least one passing
      attempt. An empty ladder is not evidence of failure, and neither is an
      incomplete escalation-history fallback whose ``attempts_count`` says the
      accepted terminal attempt is not present.

    Deliberately NOT a clause: a bare ``quality_gate_passed=False`` with no
    ladder and no typed cause. Several dispatch ports simply do not report that
    field, so treating its absence as a failure would reclassify honest runs
    that have nothing to do with this defect. Quality-gate semantics on the
    terminal are OMN-15464's seam, not this one.
    """
    if response.status != "completed":
        return False
    if response.terminal_failure_cause is not None:
        return False
    attempt_verdict = _authoritative_attempt_ladder_verdict(response)
    if attempt_verdict is False:
        return False
    return True


class ModelDelegateSkillCompleted(ModelDelegateSkillResponse):
    """Business-success terminal routed to ``delegate-skill-completed.v1``.

    Class identity — not a payload field — selects the contract's terminal
    topic: the runtime resolves ``published_events`` by the class name with the
    ``Model`` prefix removed (``DispatchResultApplier._resolve_mapped_output_topic``).
    A response that misses that map falls back to the contract's SUCCESS
    terminal, which is how a 429'd command came to publish
    ``delegate-skill-completed``.
    """

    status: Literal["completed"] = Field(default="completed")

    @model_validator(mode="after")
    def validate_completed_delegation(self) -> Self:
        """Keep the completed class/topic consistent with the composite verdict."""
        if not delegate_skill_succeeded(self):
            msg = (
                "completed delegation requires no typed terminal failure cause "
                "and at least one passing attempt when an attempt ladder is "
                "reported"
            )
            raise ValueError(msg)
        return self


class ModelDelegateSkillFailed(ModelDelegateSkillResponse):
    """Business-failure terminal routed to ``delegate-skill-failed.v1``.

    Carries the same flat payload as the completed variant — only the class
    identity differs, so downstream projections are unchanged.
    """

    status: Literal["failed", "timeout"] = Field(default="failed")

    @model_validator(mode="after")
    def validate_failed_delegation(self) -> Self:
        """Keep the failed class/topic consistent with the composite verdict."""
        if _evidence_indicates_success(self):
            msg = (
                "failed delegation requires a non-completed status, a typed "
                "terminal failure cause, or an attempt ladder with no passing "
                "attempt"
            )
            raise ValueError(msg)
        return self


def delegate_skill_terminal_from_response(
    response: ModelDelegateSkillResponse,
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
    """Return the typed terminal variant for a delegation response.

    Pure boundary conversion. Both variants serialize to the same flat payload
    the delegation projection already consumes; only the Python class identity
    selects the contract-owned terminal topic.

    When the composite verdict is negative but the port reported ``completed``,
    the status is CORRECTED to ``failed`` rather than carried onto the wire —
    the projection derives ``terminal_ok`` from ``status``, so leaving the
    port's claim intact would move the lie one hop downstream instead of
    ending it. The correction is recorded in ``error_message`` when the port
    left that empty, so the durable row explains itself.
    """
    if delegate_skill_succeeded(response):
        return ModelDelegateSkillCompleted.model_validate(
            response.model_dump(mode="python")
        )
    data = response.model_dump(mode="python")
    if data.get("status") == "completed":
        data["status"] = "failed"
        data["quality_gate_passed"] = False
        if (
            data.get("required_quality_bar") is not None
            and data.get("score_vs_required_bar") is not None
            and not data.get("failed_acceptance_criteria")
        ):
            data["failed_acceptance_criteria"] = (
                "composite delegate-skill terminal verdict failed",
            )
        if not data.get("error_message"):
            cause = response.terminal_failure_cause
            data["error_message"] = (
                f"delegation terminalized as failed: {cause.value}"
                if cause is not None
                else (
                    "delegation terminalized as failed: no attempt in the "
                    "escalation ladder passed the quality gate"
                )
            )
    return ModelDelegateSkillFailed.model_validate(data)


__all__ = [
    "ModelDelegateSkillAttemptRecord",
    "ModelDelegateSkillCompleted",
    "ModelDelegateSkillFailed",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
    "delegate_skill_succeeded",
    "delegate_skill_terminal_from_response",
    "resolve_terminal_failure_cause",
]
