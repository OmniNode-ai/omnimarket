# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegate-skill response model."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
    ModelDelegationBudgetEvidence,
    ModelDelegationBudgetRefusal,
    ModelDelegationContractEvidence,
    ModelDelegationOutputRefusal,
    ModelDelegationProvenance,
    ModelPremiumCounterfactual,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.models.delegation.credential_withheld_rung import (
    ModelCredentialWithheldRung,
)
from omnimarket.models.delegation.local_credential_refusal import (
    ModelLocalCredentialRefusal,
)

#: The terminal key that carries the ticket a delegation worked (OMN-19514).
#: The request carries it in ``metadata`` under the same name.
TICKET_ID_WIRE_KEY = "ticket_id"


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
    # OMN-18297: the two numbers a reader needs to judge an over-budget climb.
    # Recorded as a PAIR: a measurement with no budget beside it, or a budget
    # with no measurement, is not evidence that the comparison happened.
    input_tokens_measured: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Input size measured before the call, in the same character-derived "
            "units the budget is declared in (OMN-18297). None when no budget "
            "comparison was performed on this rung."
        ),
    )
    input_token_budget: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The backend's contract-declared max_grounded_input_tokens at the "
            "moment of the comparison (OMN-18297). None when the backend "
            "declares no budget, which means NOT DECLARED, never unlimited."
        ),
    )
    # OMN-18889: three facts the local dispatch port has always appended to
    # its per-rung record and this model has never declared. They reached the
    # CLI response anyway, because that payload is assembled as raw dicts and
    # never validated against this model -- so the omission was invisible
    # until the ladder was routed through the typed terminal projection, where
    # `extra="forbid"` refused the whole evidence write and the row silently
    # did not materialize. They are declared here rather than dropped at the
    # producer: each one is the reason a rung was refused, which is the single
    # most useful thing on a rung.
    acceptance_detail: str = Field(
        default="",
        description=(
            "Human-readable detail behind the accept/climb decision, e.g. the "
            "measured score against the required bar."
        ),
    )
    reasoning_preamble_rule: str | None = Field(
        default=None,
        description=(
            "Which declared rule found the seam between a leaked reasoning "
            "scratchpad and the answer (OMN-18379). None when no segmentation "
            "was attempted on this rung."
        ),
    )
    reasoning_preamble: str = Field(
        default="",
        description=(
            "What was removed from in front of the answer before any check "
            "ran (OMN-18379), retained so a refusal can be audited against "
            "exactly the text that was judged."
        ),
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
    provenance: ModelDelegationProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Typed request provenance carried unchanged onto the terminal event. "
            "None is explicit legacy/unclassified provenance."
        ),
    )
    secret_source: EnumSecretSource | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "OMN-18695. Where the provider credential for this delegation was "
            "resolved from. 'store' means the customer's own local secret "
            "store answered the contract-declared reference. Omitted for an "
            "unauthenticated backend, which is the honest record of 'no "
            "credential was resolved' rather than a false claim of a store "
            "read. The VALUE is never carried."
        ),
    )
    secret_ref: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "The secret REFERENCE the credential was resolved by -- a name the "
            "routing authority already publishes, safe to log and display. "
            "Paired with secret_source so a receipt says which key was used "
            "and from where, without ever carrying the key."
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
    response_contract_evidence: ModelDelegationContractEvidence | None = Field(
        default=None,
        description=(
            "Observed response-contract delivery and validation evidence. Present "
            "for every contract-governed terminal."
        ),
    )
    budget_evidence: ModelDelegationBudgetEvidence | None = Field(
        default=None,
        description=(
            "Requested and resolved execution budget for a dispatched terminal. "
            "An omitted CLI timeout is explicit null inside this evidence."
        ),
    )
    budget_refusal: ModelDelegationBudgetRefusal | None = Field(
        default=None,
        description=(
            "Typed pre-dispatch timeout refusal. It is present instead of "
            "budget_evidence when no execution was dispatched."
        ),
    )
    output_refusal: ModelDelegationOutputRefusal | None = Field(
        default=None,
        description=(
            "Typed refusal when the declared output contract cannot locate a "
            "safe customer deliverable."
        ),
    )
    preamble_chars: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Raw provider-response characters removed before response is returned. "
            "The response field contains only the extracted deliverable."
        ),
    )
    quality_gates_failed: list[str] = Field(default_factory=list)
    metrics: ModelDelegateSkillResponseMetrics = Field(
        default_factory=ModelDelegateSkillResponseMetrics,
    )
    # OMN-18696: the typed credential refusal from the local path, when the
    # terminal was one. Populated only for a credential refusal -- ``None`` for
    # every other terminal, success or failure, so its presence IS the fact that
    # this delegation was refused on a credential rather than failed on a
    # provider. ``error_message`` above carries the same refusal as prose for a
    # human; this carries the reference name, the remediation and the
    # non-retryable verdict as fields a skill can branch on without parsing.
    credential_refusal: ModelLocalCredentialRefusal | None = Field(
        default=None,
        description="Typed credential refusal, when the terminal was one.",
    )
    # OMN-18696 (second pass): a SIBLING of the field above and deliberately
    # not the same field. That one says this delegation was REFUSED on a
    # credential. This one says a cheaper rung was never attempted because the
    # credential it declares does not resolve -- the run may have failed for an
    # entirely unrelated reason, or, in principle, for none. Folding the two
    # together would report a quality failure as a credential refusal, which is
    # what ``test_a_non_credential_failure_carries_no_refusal_key`` exists to
    # stop. Populated only on a FAILED terminal: a run that succeeded on a
    # cheaper rung has nothing to tell the customer about a rung it never
    # needed.
    credential_withheld: ModelCredentialWithheldRung | None = Field(
        default=None,
        description=(
            "A ladder rung skipped because its declared credential does not "
            "resolve, when a failed terminal had one."
        ),
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
    # OMN-18852. Two facts a caller previously could not tell apart, because
    # only their SUM was observable as wall clock. Measured on the .201 dev
    # lane 2026-09-19: a control delegation took 181 s end to end of which the
    # inference was 1.559 s -- 99 % queue. Reported as "slow", it was not slow.
    #
    # Both are OPTIONAL and both mean NOT MEASURED when absent, never zero.
    # ``queue_wait_ms`` is derivable only when the producer stamped
    # ``published_at`` on the request; recording 0 for an unstamped request
    # would assert an empty queue nobody observed, which is the one reading
    # that would make this pair worse than having neither.
    queue_wait_ms: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
        description=(
            "Milliseconds between the command record being published and the "
            "handler picking it up. Absent means not measured."
        ),
    )
    execution_duration_ms: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
        description=(
            "Milliseconds the handler spent on this delegation, from pickup to "
            "terminal. Absent means not measured."
        ),
    )

    # OMN-19514, step 1 of 2: a CONSUMER that decodes ``ticket_id`` before any
    # producer on this package emits it (the OMN-18931 pattern on the request).
    #
    # Declaring the field outright is the OMN-18852 class, and the OMN-18868
    # Wire Compatibility Gate refuses it: the last released response model
    # forbids extras, so a producer stamping the ticket would dead-letter on
    # every consumer still carrying that release. This release decodes the key
    # and drops it; step 2 declares the field and the delegate-skill handler
    # copies the request's ticket onto the terminal, once a release carrying
    # this is out.
    #
    # Dropping is safe here in a way it was not for ``no_escalation``: the
    # ticket is attribution, not policy, so a consumer that ignores it changes
    # no behaviour. A subclass that declares the field (the terminal projection
    # model) keeps it; only a class that does not declare it drops it.
    @model_validator(mode="before")
    @classmethod
    def _tolerate_ticket_id_before_it_is_declared(cls, data: Any) -> Any:
        if (
            not isinstance(data, Mapping)
            or TICKET_ID_WIRE_KEY not in data
            or TICKET_ID_WIRE_KEY in cls.model_fields
        ):
            return data
        return {key: item for key, item in data.items() if key != TICKET_ID_WIRE_KEY}

    @model_validator(mode="before")
    @classmethod
    def derive_attempts_count_from_the_record(cls, data: Any) -> Any:
        """Derive ``attempts_count`` from the attempt list when none was given.

        OMN-19004. The field carried ``default=1``, so a terminal that supplied
        an attempt list and no count asserted ONE attempt regardless of how
        many it had just recorded. That is the defect this ticket is named for
        in its purest form: a summary field SET beside the record instead of
        DERIVED from it, disagreeing with it, and nothing noticing.

        It is not hypothetical. The bus-less dispatch port builds its terminal
        payload with the attempt list and no count, so a three-rung refusal
        published ``attempts_count=1`` next to three records.

        Derived only when the field is ABSENT. An explicit count stays
        authoritative, because a count above the list is the legitimate
        incomplete-ladder shape and the producer is entitled to state it; a
        count below the list is refused after the fact, in
        ``validate_structured_terminal_evidence``.

        An empty attempt list keeps the existing floor of one rather than
        deriving zero. A run that made no attempt at all should say zero, but
        the field is bounded at one and lowering that bound is a wire widening
        a released consumer would refuse, so it lands consumer-first and not
        here.
        """
        if not isinstance(data, dict):
            return data
        if data.get("attempts_count") is not None:
            return data
        attempts = data.get("attempts")
        if isinstance(attempts, (list, tuple)) and attempts:
            data = {**data, "attempts_count": len(attempts)}
        return data

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
        if self.budget_evidence is not None and self.budget_refusal is not None:
            msg = "budget_evidence and budget_refusal are mutually exclusive"
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

        # OMN-19004. A summary field may not contradict the record it summarises.
        #
        # ``attempts_count`` is the total-call authority and ``attempts`` is the
        # per-attempt detail, and the relationship between them is deliberately
        # ONE-directional. A count ABOVE the list is legitimate and common: the
        # escalation-history fallback carries rejected attempts only, and
        # ``_authoritative_attempt_ladder_verdict`` reads exactly that
        # inequality to decide the ladder is incomplete and withhold a verdict.
        # A count BELOW the list has no reading at all -- nothing can record
        # more attempts than it made -- so it is refused here.
        #
        # Measured on correlation ``8371bb34-3aa4-48d6-bdce-dffae3eb4b7f``: a
        # terminal reporting ``attempts_count=2`` while carrying an escalation
        # history of FOUR rejected attempts in the same payload. OMN-15464
        # closed that in the PRODUCER, via ``_truthful_attempts_count``. A
        # derivation that lives only in the producer protects only the
        # producers that call it, so the shape stayed constructible for the
        # bus-less dispatch port, a direct construction, or a future adapter --
        # the same relocation this model's other clauses exist to prevent.
        #
        # Both numbers are named in the message. A reader should not have to
        # reconstruct which two fields disagreed, or by how much.
        if self.attempts_count < len(self.attempts):
            msg = (
                f"attempts_count ({self.attempts_count}) is below the "
                f"{len(self.attempts)} attempt record(s) this terminal carries; "
                "a terminal cannot record more attempts than it counted"
            )
            raise ValueError(msg)
        return self


# Provider status classes as they appear in raw refusal text. Matched only as a
# FALLBACK, after the typed ``failure_class`` on the attempt ladder: the string
# match exists because the bus dispatch port reports the provider's raw error
# text without classifying it, and a refusal that reaches the terminal
# unclassified is exactly the case this ticket exists to stop mislabelling.
#
# Status and quota body are separate patterns (OMN-16998). Previously one regex
# alternated between them, so quota *wording* alone was sufficient to claim a
# capacity refusal -- and, having no member for anything else, an HTTP 401 fell
# through to the quota label. B7 is measured from this field, so the two must be
# corroborated independently.
_AUTH_STATUS_PATTERN = re.compile(r"\b(?:401|403)\b")
_QUOTA_STATUS_PATTERN = re.compile(r"\b429\b")
_QUOTA_BODY_PATTERN = re.compile(
    r"resource_exhausted|quota exceeded|quota_exceeded|rate limit exceeded",
    re.IGNORECASE,
)


# OMN-18696: the escalation taxonomy (``EnumDelegationFailureClass``) and the
# terminal cause vocabulary (``EnumDelegationTerminalFailureCause``) are two
# different enums whose auth members are NEAR-HOMONYMS -- ``provider_auth_failed``
# against ``auth_failed``. ``_typed_cause_claim`` compared the two by string, so
# the typed member never matched and step 1 of the resolution order silently fell
# through to the step-2 regex on free error text. A rejected credential therefore
# reached ``AUTH_FAILED`` only when the words "401" or "403" survived into a
# message -- never from the typed evidence the ladder already had.
#
# ``PROVIDER_CREDENTIAL_MISSING`` maps to ``PROVIDER_ERROR`` and NOT to
# ``AUTH_FAILED``. The core enum's ``AUTH_FAILED`` names one fact, "the provider
# rejected the credential", and an absent credential was never presented to any
# provider. Widening a published member's meaning to gain a distinction here
# would put an unpresented credential into the same bucket a rejected one is
# measured from. The fine-grained distinction lives on ``credential_refusal``
# and on ``attempts[].failure_class``, which is where AC2's three refusal codes
# are read; this coarse rollup keeps the meaning core gave it. A dedicated
# terminal member is a follow-up that has to travel through an omnibase_core
# release and a pin bump.
_TERMINAL_CAUSE_BY_FAILURE_CLASS: Mapping[str, EnumDelegationTerminalFailureCause] = {
    EnumDelegationFailureClass.PROVIDER_AUTH_FAILED.value: (
        EnumDelegationTerminalFailureCause.AUTH_FAILED
    ),
    EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING.value: (
        EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    ),
}


def _typed_cause_claim(
    attempts: Sequence[ModelDelegateSkillAttemptRecord],
) -> EnumDelegationTerminalFailureCause | None:
    """Return the first attempt's ``failure_class`` that names a terminal cause.

    A class names one either by being a terminal-cause value outright, or
    through ``_TERMINAL_CAUSE_BY_FAILURE_CLASS`` for the escalation-taxonomy
    members whose spelling differs from the terminal member they mean.
    """
    known = {member.value for member in EnumDelegationTerminalFailureCause}
    for attempt in attempts:
        raw = (attempt.failure_class or "").strip().lower()
        if raw in known:
            return EnumDelegationTerminalFailureCause(raw)
        mapped = _TERMINAL_CAUSE_BY_FAILURE_CLASS.get(raw)
        if mapped is not None:
            return mapped
    return None


def _ladder_records_an_acceptance(
    attempts: Sequence[ModelDelegateSkillAttemptRecord],
) -> bool:
    """Whether any rung on the ladder was ACCEPTED, ending the escalation.

    ``quality_gate_passed`` is the uniform field for this across both ladder
    sources: the dispatch-port path reports it directly, and the
    escalation-history path derives it from the typed
    ``acceptance_decision is ACCEPT`` (OMN-16932). Acceptance terminates the
    ladder, so at most one rung can carry it.
    """
    return any(attempt.quality_gate_passed for attempt in attempts)


def resolve_terminal_failure_cause(
    attempts: Sequence[ModelDelegateSkillAttemptRecord],
    *,
    error_message: str = "",
) -> EnumDelegationTerminalFailureCause | None:
    """Classify a delegation's terminal failure cause from its attempt ladder.

    The cause names the status class the provider actually reported. Resolution
    order (OMN-16998):

    0. **An abandoned rung is not the terminal (OMN-17979).** When the ladder
       records an ACCEPTED rung, the escalation ended in acceptance and the
       earlier rungs are escalation history, not the terminal's cause. Their
       refusal text is real, and it stays legible on ``attempts[]`` — it simply
       does not classify the outcome of a run that went on to succeed. The one
       exception is fail-closed: an outer ``error_message`` is the run's own
       report about itself, so a ladder that accepted a rung while the run still
       reported an error is classified rather than excused.
    1. **Typed evidence.** An attempt whose ``failure_class`` equals a known
       enum value is authoritative, so a port that learns to classify its own
       failures takes precedence over text matching without a change here.
    2. **Observed status.** 401/403 resolve to ``AUTH_FAILED``; a 429 carrying a
       recognised quota body resolves to ``PROVIDER_QUOTA_EXHAUSTED``.
    3. **Observed failure, unrecognised shape.** Anything else the ladder or the
       outer error actually reported resolves to ``PROVIDER_ERROR``.

    One invariant overrides step 1: **no path returns the quota cause without a
    429 in the observed response.** An uncorroborated typed quota claim degrades
    to ``PROVIDER_ERROR`` rather than entering the over-quota metric, which is
    measured from this field.

    Returns ``None`` when nothing was observed at all, and when the ladder
    accepted a rung. Silence is not a provider error, and neither is a rung the
    ladder climbed past — a successful response is forbidden from carrying a
    cause, so manufacturing one from either would make success unconstructible.
    That was the live OMN-17979 defect: an escalated run whose local rung 404'd
    and whose next rung answered at 1.0 against a 0.8 bar acquired a
    ``PROVIDER_ERROR`` here, which downgraded ``quality_gate_passed`` while the
    accepted rung's score and comparison stayed, and the resulting response
    could not be constructed at all.
    """
    if not error_message and _ladder_records_an_acceptance(attempts):
        return None

    observed = [
        text
        for text in (*(attempt.error_message for attempt in attempts), error_message)
        if text
    ]
    # Status and body must corroborate each other within a single reported
    # failure: a 429 on one rung and quota wording on another are not one event.
    quota_corroborated = any(
        _QUOTA_STATUS_PATTERN.search(text) and _QUOTA_BODY_PATTERN.search(text)
        for text in observed
    )

    typed_claim = _typed_cause_claim(attempts)
    if typed_claim is not None:
        if (
            typed_claim is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
            and not quota_corroborated
        ):
            return EnumDelegationTerminalFailureCause.PROVIDER_ERROR
        return typed_claim

    if any(_AUTH_STATUS_PATTERN.search(text) for text in observed):
        return EnumDelegationTerminalFailureCause.AUTH_FAILED
    if quota_corroborated:
        return EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    if observed:
        return EnumDelegationTerminalFailureCause.PROVIDER_ERROR
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
    "TICKET_ID_WIRE_KEY",
    "ModelDelegateSkillAttemptRecord",
    "ModelDelegateSkillCompleted",
    "ModelDelegateSkillFailed",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
    "delegate_skill_succeeded",
    "delegate_skill_terminal_from_response",
    "resolve_terminal_failure_cause",
]
