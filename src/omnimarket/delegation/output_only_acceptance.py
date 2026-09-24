# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The D1 output-only release-acceptance bar (OMN-18932, K5 of OMN-18925).

D1, accepted: a response that needs extraction before it is usable is a
release-acceptance failure, even when the extracted text is correct.

The runtime extracts on purpose. ``extract_deliverable`` cuts a scratchpad, a
planning paragraph or a trailing aside off the provider response, and the
quality gate grades what is left. That keeps a correct answer from being thrown
away at run time, and it is why OMN-18278's criterion 2 ("fail a response whose
leading segment is a reasoning trace") could not be met in the gate without
rejecting correct answers. It also means the gate's pass says nothing about
whether the model produced the artifact and only the artifact: a response
opening "Here is the scorecard:" and one that is the scorecard both score 1.0.

This module answers the question the gate does not. It judges two byte strings
that a release capture retains: the raw provider response and the bytes the
caller received. A pass means both of these hold:

* **No extraction was needed.** The caller's bytes are a contiguous slice of the
  raw provider bytes, and nothing outside that slice except whitespace and the
  one declared render start marker the prompt asked the model to open with.
* **The slice is the requested artifact.** It is non-empty, it does not open
  with declared planning prose or carry a reasoning-trace terminator, its final
  paragraph is not a declared self-review or sign-off, and it is well formed
  for its declared shape (JSON that parses as one value and satisfies the
  declared schema; Markdown with balanced code fences; plain text that is not
  code and fits declared word limits).

Every phrase it matches is declared in ``task_class_contracts.v1.yaml``
(``reasoning_preamble.lead_in_phrases`` and ``closing_trace_tags``, and
``output_only_acceptance.trailing_self_review_openers``), not written here.

Missing raw provider bytes fail closed. A verdict computed from the caller's
bytes alone cannot tell a clean response from an extracted one, so it is
refused as ``raw_provider_bytes_absent`` rather than passed.

This module changes no runtime verdict. The gate, extraction and the terminal
are unchanged; the bar is read by the release-acceptance runner and by K5's
evidence, where OMN-18278's criterion 2 now resolves.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum, unique

from omnibase_core.models.delegation.wire import EnumDelegationOutputShape
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.delegation.deliverable_extraction import ModelDeliverableContract
from omnimarket.delegation.response_contract_conformance import (
    schema_violation_reasons,
)
from omnimarket.inference.task_class_authority import (
    ModelOutputOnlyAcceptancePolicy,
    ModelReasoningPreamblePolicy,
    load_task_class_authority,
)

#: How far into the caller's bytes a declared lead-in phrase is searched for.
#: The same window the reasoning-preamble segmenter uses, so "opens with
#: planning prose" means one thing in both places.
_LEAD_IN_WINDOW_CHARS: int = 120

_FENCE_LINE = re.compile(r"^[ \t]{0,3}(```|~~~)")


@unique
class EnumOutputOnlyRefusal(StrEnum):
    """Why a response fails the output-only bar. One value per distinct cause."""

    RAW_PROVIDER_BYTES_ABSENT = "raw_provider_bytes_absent"
    CALLER_BYTES_NOT_A_RAW_SLICE = "caller_bytes_not_a_raw_provider_slice"
    EXTRACTION_REQUIRED_LEADING_TEXT = "extraction_required_leading_text"
    EXTRACTION_REQUIRED_TRAILING_TEXT = "extraction_required_trailing_text"
    EMPTY_DELIVERABLE = "empty_deliverable"
    PLANNING_PROSE = "planning_prose_in_caller_bytes"
    TRAILING_SELF_REVIEW = "trailing_self_review_in_caller_bytes"
    MALFORMED_STRUCTURE = "malformed_structure"


class ModelOutputOnlyVerdict(BaseModel):
    """The bar's verdict, with the hashes a release receipt binds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    refusals: tuple[EnumOutputOnlyRefusal, ...]
    details: tuple[str, ...]
    output_shape: EnumDelegationOutputShape
    caller_sha256: str
    caller_chars: int = Field(ge=0)
    raw_sha256: str | None
    raw_chars: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _accepted_iff_no_refusal(self) -> ModelOutputOnlyVerdict:
        if self.accepted is bool(self.refusals):
            raise ValueError("accepted must be true exactly when nothing refused")
        if len(self.details) != len(self.refusals):
            raise ValueError("every refusal carries exactly one detail")
        return self


def evaluate_output_only(
    *,
    raw_response: str | None,
    caller_bytes: str,
    contract: ModelDeliverableContract,
) -> ModelOutputOnlyVerdict:
    """Judge one response against the D1 output-only bar.

    Args:
        raw_response: The provider's full response text, byte for byte, or
            ``None`` when no carrier retained it. ``None`` is refused.
        caller_bytes: Exactly what the caller received as the answer.
        contract: The one resolved deliverable contract the request ran under
            (``resolve_task_class_deliverable_contract``).
    """
    authority = load_task_class_authority()
    if authority.reasoning_preamble is None:
        raise ValueError("output-only bar requires the reasoning_preamble policy")
    if authority.output_only_acceptance is None:
        raise ValueError("output-only bar requires the output_only_acceptance policy")
    found: list[tuple[EnumOutputOnlyRefusal, str]] = []
    found.extend(_extraction_refusals(raw_response, caller_bytes, contract))
    found.extend(
        _artifact_refusals(
            caller_bytes,
            contract,
            authority.reasoning_preamble,
            authority.output_only_acceptance,
        )
    )
    return ModelOutputOnlyVerdict(
        accepted=not found,
        refusals=tuple(reason for reason, _ in found),
        details=tuple(detail for _, detail in found),
        output_shape=contract.output_shape,
        caller_sha256=_sha256(caller_bytes),
        caller_chars=len(caller_bytes),
        raw_sha256=None if raw_response is None else _sha256(raw_response),
        raw_chars=None if raw_response is None else len(raw_response),
    )


def _extraction_refusals(
    raw_response: str | None,
    caller_bytes: str,
    contract: ModelDeliverableContract,
) -> list[tuple[EnumOutputOnlyRefusal, str]]:
    if raw_response is None:
        return [
            (
                EnumOutputOnlyRefusal.RAW_PROVIDER_BYTES_ABSENT,
                "no raw provider response was retained, so extraction cannot be "
                "ruled out",
            )
        ]
    if not caller_bytes.strip():
        return []
    start = raw_response.rfind(caller_bytes)
    if start < 0:
        return [
            (
                EnumOutputOnlyRefusal.CALLER_BYTES_NOT_A_RAW_SLICE,
                "the caller's bytes do not occur in the raw provider response",
            )
        ]
    refusals: list[tuple[EnumOutputOnlyRefusal, str]] = []
    leading = raw_response[:start]
    if not _leading_is_declared_opening(leading, contract):
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,
                f"{len(leading.strip())} non-whitespace chars precede the "
                "caller's bytes in the raw response",
            )
        )
    trailing = raw_response[start + len(caller_bytes) :]
    if trailing.strip():
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_TRAILING_TEXT,
                f"{len(trailing.strip())} non-whitespace chars follow the "
                "caller's bytes in the raw response",
            )
        )
    return refusals


def _leading_is_declared_opening(
    leading: str, contract: ModelDeliverableContract
) -> bool:
    """Whitespace, or exactly the render start marker the prompt asked for."""
    stripped = leading.strip()
    if not stripped:
        return True
    return (
        contract.output_shape is not EnumDelegationOutputShape.JSON
        and contract.render_start_marker is not None
        and stripped == contract.render_start_marker
    )


def _artifact_refusals(
    caller_bytes: str,
    contract: ModelDeliverableContract,
    preamble_policy: ModelReasoningPreamblePolicy,
    policy: ModelOutputOnlyAcceptancePolicy,
) -> list[tuple[EnumOutputOnlyRefusal, str]]:
    if not caller_bytes.strip():
        return [
            (
                EnumOutputOnlyRefusal.EMPTY_DELIVERABLE,
                "the caller received no non-whitespace text",
            )
        ]
    refusals: list[tuple[EnumOutputOnlyRefusal, str]] = []
    planning = _planning_prose(caller_bytes, preamble_policy)
    if planning is not None:
        refusals.append((EnumOutputOnlyRefusal.PLANNING_PROSE, planning))
    self_review = _trailing_self_review(caller_bytes, policy)
    if self_review is not None:
        refusals.append((EnumOutputOnlyRefusal.TRAILING_SELF_REVIEW, self_review))
    malformed = _malformed_structure(caller_bytes, contract)
    if malformed is not None:
        refusals.append((EnumOutputOnlyRefusal.MALFORMED_STRUCTURE, malformed))
    return refusals


def _planning_prose(
    caller_bytes: str, policy: ModelReasoningPreamblePolicy
) -> str | None:
    window = caller_bytes.lstrip()[:_LEAD_IN_WINDOW_CHARS].lower()
    for phrase in policy.lead_in_phrases:
        if phrase.lower() in window:
            return f"opens with declared lead-in phrase {phrase!r}"
    lowered = caller_bytes.lower()
    for tag in policy.closing_trace_tags:
        if tag.lower() in lowered:
            return f"carries declared reasoning-trace terminator {tag!r}"
    return None


def _trailing_self_review(
    caller_bytes: str, policy: ModelOutputOnlyAcceptancePolicy
) -> str | None:
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", caller_bytes) if p.strip()]
    final = paragraphs[-1].strip().lower()
    for opener in policy.trailing_self_review_openers:
        if final.startswith(opener):
            return f"final paragraph opens with declared self-review {opener!r}"
    return None


def _malformed_structure(
    caller_bytes: str, contract: ModelDeliverableContract
) -> str | None:
    if contract.output_shape is EnumDelegationOutputShape.JSON:
        try:
            candidate = json.loads(caller_bytes)
        except json.JSONDecodeError as exc:
            return f"caller bytes are not exactly one JSON value: {exc.msg}"
        if contract.json_schema is None:
            return "JSON output shape declares no schema to satisfy"
        violations = schema_violation_reasons(candidate, contract.json_schema)
        if violations:
            return f"JSON violates the declared schema: {violations[0]}"
        return None
    fences = sum(
        1 for line in caller_bytes.splitlines() if _FENCE_LINE.match(line) is not None
    )
    if contract.output_shape is EnumDelegationOutputShape.MARKDOWN:
        if fences % 2:
            return f"unbalanced code fence ({fences} fence lines)"
        return None
    if fences:
        return "plain-text deliverable carries a code fence"
    constraints = contract.plain_text_constraints
    if constraints is not None:
        words = len(re.findall(r"\S+", caller_bytes))
        if not constraints.min_words <= words <= constraints.max_words:
            return (
                f"{words} words, outside the declared "
                f"{constraints.min_words}-{constraints.max_words}"
            )
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EnumOutputOnlyRefusal",
    "ModelOutputOnlyVerdict",
    "evaluate_output_only",
]
