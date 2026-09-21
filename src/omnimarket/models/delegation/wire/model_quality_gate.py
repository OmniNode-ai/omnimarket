# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation quality gate wire DTOs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from omnibase_core.models.delegation.wire.model_quality_gate import (
    EnumQualityRuleEnforcement,
    ModelQualityRuleEvaluation,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.models.delegation.wire.model_delegation_request import (
    EnumQualityContractMode,
)

EnumQualityGateCategory = Literal["pass", "fail_deterministic", "fail_heuristic"]

# Canonical ``score_source`` identifiers recorded on ``ModelQualityGateResult``
# (OMN-13470/OMN-13959). Kept here on the shared wire model so both the quality
# gate reducer (which SETS the value) and the acceptance-decision callers — the
# bus orchestrator ``handle_gate_result`` and the bus-less local dispatch port
# ``_is_quality_accepted`` — reference the same constant without a magic string
# or a cross-node handler import.
#
#   SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE — a VERIFIABLE task class cleared the
#     deterministic acceptance FLOOR but the LLM judge adequacy score was NOT
#     combined (JUDGE_FAILED: judge unreachable / throttled, or no judge run).
#   SCORE_SOURCE_COMBINED — the LLM-judge adequacy score WAS combined into the
#     graded score (judge reachable).
#
# The distinction is load-bearing for OMN-13959: when the judge is unavailable a
# verifiable class can only ever produce SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
# and the combined-score ``required_bar`` (0.85) is structurally un-meetable
# without the judge band — so acceptance must fall back to the deterministic
# floor verdict instead of failing valid local output during a cloud-judge outage.
SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE = "deterministic_acceptance"
SCORE_SOURCE_COMBINED = "combined"

# OMN-19016/OMN-19056: the verdict prefix a failure reason carries when the
# refusal is a deterministic function of the response's SHAPE. It lives on the
# shared wire model for the same reason the two ``SCORE_SOURCE_*`` identifiers
# above do: the quality-gate reducer WRITES reasons carrying it and the
# acceptance-decision callers READ them, and the two must not each hold their
# own spelling of the same token.
#
# It travels INSIDE ``failure_reasons``, which is a plain ``tuple[str, ...]``
# every released consumer back to v0.4.166 already declares and accepts. That
# is what makes the futility verdict expressible without adding a key to this
# model -- see ``ModelQualityGateResult.no_rung_can_satisfy``.
SHAPE_REFUSED_VERDICT_PREFIX = "SHAPE_REFUSED"

# The wire key OMN-19016 briefly emitted and OMN-19056 withdrew. Named once, so
# the tolerance validator and the tests that pin it cannot drift apart.
NO_RUNG_CAN_SATISFY_WIRE_KEY = "no_rung_can_satisfy"


class ModelQualityGateInput(BaseModel):
    """Gate input: LLM response content and expected quality markers."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this input back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification for type-specific checks.",
    )
    llm_response_content: str = Field(
        ...,
        description="The raw LLM response to evaluate.",
    )
    expected_markers: tuple[str, ...] = Field(
        default=(),
        description="Strings expected in the response for the task type.",
    )
    min_response_length: int = Field(
        default=60,
        description="Minimum acceptable response length in characters.",
    )
    dod_deterministic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Deterministic DoD check names from the task-class contract (OMN-10614). "
            "These checks BLOCK delegation result injection on failure."
        ),
    )
    dod_heuristic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Heuristic DoD check names from the task-class contract (OMN-10614). "
            "These checks escalate per contract policy on failure."
        ),
    )
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description="How request-level acceptance criteria interact with task-class DoD.",
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description="Request-level quality checks enforced by the quality gate.",
    )


class ModelQualityGateResult(BaseModel):
    """Gate output: pass/fail verdict, score, failure reasons, and fallback flag."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    passed: bool = Field(
        ..., description="Whether the LLM response passed the quality gate."
    )
    fail_category: EnumQualityGateCategory = Field(
        default="pass",
        description=(
            "Structured outcome: 'pass', 'fail_deterministic' (hard block), "
            "or 'fail_heuristic' (escalate per contract policy)."
        ),
    )
    quality_score: float = Field(..., description="Quality score from 0.0 to 1.0.")
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Tuple of human-readable failure reason strings.",
    )
    fallback_recommended: bool = Field(
        default=False,
        description="Whether fallback to Claude is recommended.",
    )
    score_source: str = Field(
        default="",
        description="Authority that produced actual_score when applicable.",
    )
    acceptance_version: str = Field(
        default="",
        description="Deterministic acceptance evaluator version when applicable.",
    )
    corpus_hash: str = Field(
        default="",
        description="Stable hash of the deterministic acceptance corpus/check set.",
    )
    validator_or_artifact_hash: str = Field(
        default="",
        description="Stable hash of the validator or delegated artifact evaluated.",
    )
    acceptance_command: str = Field(
        default="",
        description="Replay command or command identity for deterministic acceptance.",
    )
    actual_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Authority score produced by the acceptance source.",
    )
    pass_: bool | None = Field(
        default=None,
        alias="pass",
        description="Authority pass/fail verdict produced by the acceptance source.",
    )
    failure_cases: tuple[str, ...] = Field(
        default=(),
        description="Deterministic acceptance failure cases.",
    )
    rule_evaluations: tuple[ModelQualityRuleEvaluation, ...] = Field(
        default=(),
        description=(
            "Per-rule verdicts (OMN-18295): each declared check's own result, "
            "the threshold it applied, and whether it was entitled to veto. "
            "Recorded for passing rules too, so a reader can tell a rule that "
            "passed from one that never ran - and can see that a 'scored' "
            "miss did not decide the outcome the bar decided."
        ),
    )
    ungrounded_identifiers: tuple[str, ...] = Field(
        default=(),
        description=(
            "Identifiers the response cited that occur nowhere in the "
            "grounding source and were not marked unverified (OMN-18297). "
            "Rendered as '<class_name>:<identifier>'. Empty when the "
            "identifier-grounding check passed, was not declared by the task "
            "class, or was skipped for want of a grounding source - the "
            "skipped case is named in skipped_checks, so an empty tuple here "
            "is never read as proof the check ran."
        ),
    )
    skipped_checks: tuple[str, ...] = Field(
        default=(),
        description=(
            "Deterministic DoD checks that were NOT evaluated because no "
            "acceptance-command executor is wired for them (OMN-13850). Recorded "
            "as durable evidence of the unevaluated status; excluded from the "
            "deterministic passed/total fraction so a check with no executor "
            "cannot report a phantom 'passed'."
        ),
    )
    reasoning_preamble: str = Field(
        default="",
        description=(
            "The leaked reasoning scratchpad removed from in front of the "
            "answer before any check ran (OMN-18379), verbatim. Retained so a "
            "verdict can be audited against exactly the text it judged. Empty "
            "when no preamble was found."
        ),
    )
    reasoning_preamble_rule: str = Field(
        default="",
        description=(
            "Which declared boundary rule separated preamble from answer "
            "(OMN-18379): 'unpaired_closing_tag', 'answer_marker', "
            "'markdown_header', 'fenced_block', or 'no_boundary_found' when "
            "the whole response was verified. Empty ONLY on a result produced "
            "before this field existed - it is never the segmenter's own "
            "answer, so 'the segmenter did not run' stays distinguishable from "
            "'there was nothing to strip'."
        ),
    )
    finish_reason: EnumProviderFinishReason = Field(
        default=EnumProviderFinishReason.ABSENT,
        description=(
            "Why the provider stopped generating, as the gate was told it "
            "(OMN-18278). Recorded on every verdict, pass and fail alike, so a "
            "reader can tell a response the gate KNEW was complete from one it "
            "was told nothing about. 'absent' is the second of those: it means "
            "no signal reached this evaluation - the bus path carries none "
            "today - and is never evidence that the response finished. Only "
            "'length' vetoes."
        ),
    )

    # OMN-19056: ACCEPT the withdrawn key, never emit it. This is the consumer
    # half of the consumer-first rule and it is the whole reason the next
    # release can carry the field back.
    #
    # ``no_rung_can_satisfy`` shipped as a FIELD on this model in OMN-19016
    # (omnimarket#2755, dev 0.4.175). This model declares ``extra="forbid"``
    # and the last released omnimarket, v0.4.166, has no such field, so every
    # deployed consumer at the release refuses the payload outright:
    # "no_rung_can_satisfy: Extra inputs are not permitted". That is OMN-18852
    # one field later, and the OMN-18868 wire gate caught it on its first day.
    #
    # ``exclude_if`` -- the OMN-18852 remedy -- does NOT answer this one, and
    # the gate says so in its own docstring: it grades the MAXIMAL shape a
    # producer can emit, so a field excluded while unset is still a finding,
    # and it has no waiver list. It is right to. ``exclude_if`` only ever
    # bought a quiet window, and this field's default is emitted on every dump,
    # so there is no window here at all.
    #
    # So the field is gone and the value is DERIVED below instead. What stays
    # is this validator: a payload that still carries the key is accepted and
    # the key is dropped, rather than refused. Two reasons, and the second is
    # the load-bearing one.
    #
    #   1. A producer running dev 0.4.175 exactly -- never released, never
    #      deployed, but reachable by anyone on that commit -- keeps working
    #      against a consumer carrying this model.
    #   2. The NEXT release becomes the consumer that TOLERATES the key. That
    #      is the release floor step 2 needs: once it is deployed, re-adding
    #      ``no_rung_can_satisfy`` as a real emitted field passes the gate,
    #      because the released consumer's own ``model_validate`` no longer
    #      raises on it. Withdrawing the field without this validator would
    #      leave step 2 blocked by the same gate forever.
    #
    # The dropped key is not read. The derivation below is the authority, and
    # it agrees with the value a 0.4.175 producer would have sent, because both
    # compute the same predicate over the same ``failure_reasons``.
    @model_validator(mode="before")
    @classmethod
    def _tolerate_withdrawn_no_rung_can_satisfy(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and NO_RUNG_CAN_SATISFY_WIRE_KEY in data:
            return {
                key: value
                for key, value in data.items()
                if key != NO_RUNG_CAN_SATISFY_WIRE_KEY
            }
        return data

    # OMN-19016's verdict, OMN-19056's carriage. Deliberately a plain
    # ``property`` and deliberately NOT a ``computed_field``.
    #
    # A ``computed_field`` would put the key back on the wire and break the
    # released consumer again -- and it would do so INVISIBLY to the OMN-18868
    # gate, which reads ``model_fields`` and never ``model_computed_fields``.
    # That combination, a re-break the gate cannot see, is worse than the
    # original defect, so the shape is pinned by a test rather than left to
    # this comment.
    #
    # Nothing is lost by deriving it. The value was never independent
    # information: the producer computed exactly this predicate over exactly
    # these reasons before assigning it, and ``failure_reasons`` is a field
    # every released consumer back to v0.4.166 already declares, accepts and
    # carries the ``SHAPE_REFUSED`` prefix through untouched. So every consumer
    # that could have read the field can compute the verdict instead, and a
    # consumer old enough to know neither keeps the pre-OMN-19016 climb-always
    # behaviour, exactly as the withdrawn field's own description promised.
    #
    # ``all``, not ``any``, and the asymmetry is OMN-19016's, preserved
    # verbatim: one climbable reason beside a shape refusal means a costlier
    # rung still has something to cure, so the ladder keeps its escalation.
    # Only when the shape is the WHOLE objection is climbing provably futile.
    # An empty reason tuple is not a veto at all and yields ``False``, so a
    # passing result can never be reported unsatisfiable.
    @property
    def no_rung_can_satisfy(self) -> bool:
        """Whether every reason refusing this response is a shape refusal."""
        return bool(self.failure_reasons) and all(
            reason.startswith(SHAPE_REFUSED_VERDICT_PREFIX)
            for reason in self.failure_reasons
        )


# OMN-18295: ``ModelQualityRuleEvaluation`` and its enforcement vocabulary are
# defined ONCE, in omnibase_core, because the delegation TERMINAL
# (``ModelDelegationResult``, also core) carries them out to the gateway. A
# second definition here would be two wire contracts for one wire field. They
# are re-exported from this module so every existing importer keeps its path.
__all__: list[str] = [
    "NO_RUNG_CAN_SATISFY_WIRE_KEY",
    "SCORE_SOURCE_COMBINED",
    "SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE",
    "SHAPE_REFUSED_VERDICT_PREFIX",
    "EnumProviderFinishReason",
    "EnumQualityGateCategory",
    "EnumQualityRuleEnforcement",
    "ModelQualityGateInput",
    "ModelQualityGateResult",
    "ModelQualityRuleEvaluation",
]
