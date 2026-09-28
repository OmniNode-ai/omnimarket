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
  For a JSON deliverable the raw response is exactly one JSON value and it is
  the caller's value, so a runtime re-serialization of that one value is not
  extraction (OMN-19385).
* **The slice is the requested artifact.** It is non-empty, it does not open
  with declared planning prose or carry a reasoning-trace terminator, its final
  paragraph is not a declared self-review or sign-off, and it is well formed
  for its declared shape (JSON that parses as one value and satisfies the
  declared schema; Markdown with balanced code fences; plain text that is not
  code and fits declared word limits).

Every phrase it matches is declared in ``task_class_contracts.v1.yaml``
(``reasoning_preamble.lead_in_phrases`` and ``closing_trace_tags``, and
``output_only_acceptance.trailing_self_review_openers``), not written here.

The "no extraction" half is judged from one of two evidence bases, and the
verdict records which:

* ``raw_provider_bytes``: the provider's full response, byte for byte. This is
  the basis K5's final evidence requires, and the only one that can prove a
  JSON deliverable had nothing after it.
* ``runtime_extraction_count``: the runtime's own count of characters it cut
  from the front of the response (``preamble_chars`` on the terminal), which is
  what a live terminal carries today. It proves the leading half. It proves the
  trailing half only for text shapes, whose extractor keeps everything to the
  end of the response; for JSON the trailing half is unobservable and the bar
  refuses with ``extraction_evidence_incomplete``.

With neither, the bar fails closed with ``raw_provider_bytes_absent``: the
caller's bytes alone cannot tell a clean response from an extracted one.

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
    EXTRACTION_EVIDENCE_INCOMPLETE = "extraction_evidence_incomplete"


@unique
class EnumOutputOnlyEvidenceBasis(StrEnum):
    """What the "no extraction was needed" half of a verdict was judged from."""

    RAW_PROVIDER_BYTES = "raw_provider_bytes"
    RUNTIME_EXTRACTION_COUNT = "runtime_extraction_count"
    ABSENT = "absent"


class ModelOutputOnlyVerdict(BaseModel):
    """The bar's verdict, with the hashes a release receipt binds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    refusals: tuple[EnumOutputOnlyRefusal, ...]
    details: tuple[str, ...]
    output_shape: EnumDelegationOutputShape
    evidence_basis: EnumOutputOnlyEvidenceBasis
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
    runtime_leading_chars: int | None = None,
) -> ModelOutputOnlyVerdict:
    """Judge one response against the D1 output-only bar.

    Args:
        raw_response: The provider's full response text, byte for byte, or
            ``None`` when no carrier retained it.
        caller_bytes: Exactly what the caller received as the answer.
        contract: The one resolved deliverable contract the request ran under
            (``resolve_task_class_deliverable_contract``).
        runtime_leading_chars: The runtime's own count of characters cut from
            the front of the response, used only when ``raw_response`` is
            ``None``. ``None`` with no raw bytes is refused.
    """
    authority = load_task_class_authority()
    if authority.reasoning_preamble is None:
        raise ValueError("output-only bar requires the reasoning_preamble policy")
    if authority.output_only_acceptance is None:
        raise ValueError("output-only bar requires the output_only_acceptance policy")
    # Surrounding whitespace is not graded, by either half: both judge the
    # same ``answer``. The hashes on the verdict are over the exact bytes.
    answer = caller_bytes.strip()
    found: list[tuple[EnumOutputOnlyRefusal, str]] = []
    if raw_response is not None:
        basis = EnumOutputOnlyEvidenceBasis.RAW_PROVIDER_BYTES
        found.extend(_extraction_refusals(raw_response, answer, contract))
    elif runtime_leading_chars is not None:
        basis = EnumOutputOnlyEvidenceBasis.RUNTIME_EXTRACTION_COUNT
        found.extend(_counted_extraction_refusals(runtime_leading_chars, contract))
    else:
        basis = EnumOutputOnlyEvidenceBasis.ABSENT
        found.append(
            (
                EnumOutputOnlyRefusal.RAW_PROVIDER_BYTES_ABSENT,
                "neither the raw provider response nor the runtime's extraction "
                "count was retained, so extraction cannot be ruled out",
            )
        )
    found.extend(
        _artifact_refusals(
            answer,
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
        evidence_basis=basis,
        caller_sha256=_sha256(caller_bytes),
        caller_chars=len(caller_bytes),
        raw_sha256=None if raw_response is None else _sha256(raw_response),
        raw_chars=None if raw_response is None else len(raw_response),
    )


def _counted_extraction_refusals(
    runtime_leading_chars: int, contract: ModelDeliverableContract
) -> list[tuple[EnumOutputOnlyRefusal, str]]:
    """Judge extraction from the runtime's count of leading characters cut.

    The only permitted cut is exactly the declared render start marker line
    (the marker plus its newline). A longer cut cannot be told apart from text
    before the marker, so it is refused. A text shape's extractor keeps every
    character after its start, so nothing can have been cut from the end; a
    JSON extractor stops at the end of the value, so the trailing half of a
    JSON response is unobservable from this count and is refused.
    """
    if runtime_leading_chars < 0:
        raise ValueError("runtime_leading_chars must be non-negative")
    refusals: list[tuple[EnumOutputOnlyRefusal, str]] = []
    permitted = {0}
    if (
        contract.output_shape is not EnumDelegationOutputShape.JSON
        and contract.render_start_marker is not None
    ):
        permitted.add(len(contract.render_start_marker) + 1)
    if runtime_leading_chars not in permitted:
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,
                f"the runtime cut {runtime_leading_chars} leading chars; only "
                f"{sorted(permitted)} is the declared opening",
            )
        )
    if contract.output_shape is EnumDelegationOutputShape.JSON:
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_EVIDENCE_INCOMPLETE,
                "a JSON extractor stops at the end of the value, so text after "
                "it is unobservable without the raw provider response",
            )
        )
    return refusals


def _extraction_refusals(
    raw_response: str,
    answer: str,
    contract: ModelDeliverableContract,
) -> list[tuple[EnumOutputOnlyRefusal, str]]:
    """Judge extraction from the raw provider response itself.

    The accepted form is exact, not searched for: the raw response is the
    caller's bytes, optionally opened by the declared render start marker line,
    with nothing else but surrounding whitespace. Anything else is refused,
    and the refusal names where the extra text sits: the caller's bytes are
    located at their FIRST occurrence, so a response that repeats the answer
    is refused for trailing text rather than accepted.
    """
    if not answer:
        return []
    if _is_exact_output(raw_response, answer, contract):
        return []
    start = raw_response.find(answer)
    if start < 0:
        return [
            (
                EnumOutputOnlyRefusal.CALLER_BYTES_NOT_A_RAW_SLICE,
                "the caller's bytes are not one contiguous slice of the raw "
                "provider response",
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
    trailing = raw_response[start + len(answer) :]
    if trailing.strip():
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_TRAILING_TEXT,
                f"{len(trailing.strip())} non-whitespace chars follow the "
                "caller's bytes in the raw response",
            )
        )
    if not refusals:
        # The only way to reach here is surrounding whitespace the exact form
        # does not allow, such as text-free lines between the marker and the
        # answer. Name it rather than accept a form the bar did not declare.
        refusals.append(
            (
                EnumOutputOnlyRefusal.EXTRACTION_REQUIRED_LEADING_TEXT,
                "the raw response is not exactly the caller's bytes behind the "
                "declared opening",
            )
        )
    return refusals


def _is_exact_output(
    raw_response: str, answer: str, contract: ModelDeliverableContract
) -> bool:
    """Raw bytes equal the answer, or the marker line then the answer.

    For a JSON deliverable the comparison is by value (OMN-19385): the raw
    response is exactly one JSON value, with nothing around it but whitespace,
    and it is the caller's value. A runtime that re-serializes the one value
    the provider returned (the in-process port hands the caller canonical
    JSON) has extracted nothing, and a byte comparison would refuse a clean
    answer for our formatting. Values are compared through a canonical
    encoding, so ``true`` and ``1`` stay different values.
    """
    raw = raw_response.strip()
    if raw == answer:
        return True
    marker = contract.render_start_marker
    if contract.output_shape is EnumDelegationOutputShape.JSON:
        raw_value = _sole_json_value(raw)
        answer_value = _sole_json_value(answer)
        return (
            raw_value is not _NOT_ONE_JSON_VALUE
            and answer_value is not _NOT_ONE_JSON_VALUE
            and _canonical_json(raw_value) == _canonical_json(answer_value)
        )
    if marker is None:
        return False
    head, newline, rest = raw.partition("\n")
    return bool(newline) and head.strip() == marker and rest.strip() == answer


_NOT_ONE_JSON_VALUE = object()


def _sole_json_value(text: str) -> object:
    """The one JSON value *text* is, or the sentinel when it is not exactly one."""
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return _NOT_ONE_JSON_VALUE
    return value if end == len(text) else _NOT_ONE_JSON_VALUE


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
    answer: str,
    contract: ModelDeliverableContract,
    preamble_policy: ModelReasoningPreamblePolicy,
    policy: ModelOutputOnlyAcceptancePolicy,
) -> list[tuple[EnumOutputOnlyRefusal, str]]:
    if not answer:
        return [
            (
                EnumOutputOnlyRefusal.EMPTY_DELIVERABLE,
                "the caller received no non-whitespace text",
            )
        ]
    refusals: list[tuple[EnumOutputOnlyRefusal, str]] = []
    planning = _planning_prose(answer, preamble_policy)
    if planning is not None:
        refusals.append((EnumOutputOnlyRefusal.PLANNING_PROSE, planning))
    self_review = _trailing_self_review(answer, policy)
    if self_review is not None:
        refusals.append((EnumOutputOnlyRefusal.TRAILING_SELF_REVIEW, self_review))
    malformed = _malformed_structure(answer, contract)
    if malformed is not None:
        refusals.append((EnumOutputOnlyRefusal.MALFORMED_STRUCTURE, malformed))
    return refusals


def _planning_prose(answer: str, policy: ModelReasoningPreamblePolicy) -> str | None:
    window = answer[:_LEAD_IN_WINDOW_CHARS].lower()
    for phrase in policy.lead_in_phrases:
        if phrase.lower() in window:
            return f"opens with declared lead-in phrase {phrase!r}"
    lowered = answer.lower()
    for tag in policy.closing_trace_tags:
        if tag.lower() in lowered:
            return f"carries declared reasoning-trace terminator {tag!r}"
    return None


def _trailing_self_review(
    answer: str, policy: ModelOutputOnlyAcceptancePolicy
) -> str | None:
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", answer) if p.strip()]
    final = paragraphs[-1].strip().lower()
    for opener in policy.trailing_self_review_openers:
        if final.startswith(opener):
            return f"final paragraph opens with declared self-review {opener!r}"
    return None


def _malformed_structure(answer: str, contract: ModelDeliverableContract) -> str | None:
    if contract.output_shape is EnumDelegationOutputShape.JSON:
        try:
            candidate = json.loads(answer)
        except json.JSONDecodeError as exc:
            return f"caller bytes are not exactly one JSON value: {exc.msg}"
        if contract.json_schema is None:
            return "JSON output shape declares no schema to satisfy"
        violations = schema_violation_reasons(candidate, contract.json_schema)
        if violations:
            return f"JSON violates the declared schema: {violations[0]}"
        return None
    fences = sum(
        1 for line in answer.splitlines() if _FENCE_LINE.match(line) is not None
    )
    if contract.output_shape is EnumDelegationOutputShape.MARKDOWN:
        if fences % 2:
            return f"unbalanced code fence ({fences} fence lines)"
        return None
    if fences:
        return "plain-text deliverable carries a code fence"
    constraints = contract.plain_text_constraints
    if constraints is not None:
        words = len(re.findall(r"\S+", answer))
        if not constraints.min_words <= words <= constraints.max_words:
            return (
                f"{words} words, outside the declared "
                f"{constraints.min_words}-{constraints.max_words}"
            )
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EnumOutputOnlyEvidenceBasis",
    "EnumOutputOnlyRefusal",
    "ModelOutputOnlyVerdict",
    "evaluate_output_only",
]
