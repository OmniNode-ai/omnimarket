# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Extract a customer deliverable from a response under its declared shape."""

from __future__ import annotations

import json
import re
from enum import StrEnum, unique
from hashlib import sha256

import jsonschema
import jsonschema.validators
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.delegation.response_contract_conformance import (
    schema_violation_reasons,
)
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.inference.task_class_authority import (
    resolve_delegation_output_authority,
    resolve_task_class_output_contract,
)


@unique
class EnumDeliverableExtractionRefusal(StrEnum):
    """Why extraction declined to return a deliverable."""

    AMBIGUOUS_UNMARKED = "ambiguous_unmarked_deliverable"
    NO_SCHEMA_CONFORMING_JSON = "no_schema_conforming_json"
    BELOW_SHARE_FLOOR = "deliverable_share_below_floor"
    PLAIN_TEXT_CONSTRAINT_VIOLATION = "plain_text_constraint_violation"


@unique
class EnumDeliverableBoundaryMode(StrEnum):
    """Declared ways to locate a text deliverable without prose guessing."""

    MARKER = "marker"
    FINAL_PARAGRAPH = "final_paragraph"


class ModelPlainTextOutputConstraints(BaseModel):
    """Structural restrictions for an explicitly declared plain-text artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_words: int = Field(ge=1)
    max_words: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_word_range(self) -> ModelPlainTextOutputConstraints:
        if self.min_words > self.max_words:
            raise ValueError("min_words cannot exceed max_words")
        return self


class ModelDeliverableContract(BaseModel):
    """Declared output shape, boundary and minimum answer share."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_shape: EnumDelegationOutputShape
    min_deliverable_share: float = Field(gt=0.0, le=1.0)
    markers: tuple[str, ...] = ()
    json_schema: dict[str, object] | None = None
    render_start_marker: str | None = None
    boundary_mode: EnumDeliverableBoundaryMode = EnumDeliverableBoundaryMode.MARKER
    plain_text_constraints: ModelPlainTextOutputConstraints | None = None

    @model_validator(mode="after")
    def _validate_shape_requirements(self) -> ModelDeliverableContract:
        if self.output_shape is EnumDelegationOutputShape.JSON:
            if self.json_schema is None:
                raise ValueError("json output_shape requires json_schema")
            if self.markers:
                raise ValueError("json output_shape must not declare text markers")
        elif self.json_schema is not None:
            raise ValueError("text output shapes must not declare json_schema")
        elif (
            self.boundary_mode is EnumDeliverableBoundaryMode.MARKER
            and not self.markers
        ):
            raise ValueError("text output shapes require at least one declared marker")
        elif self.boundary_mode is EnumDeliverableBoundaryMode.FINAL_PARAGRAPH:
            if self.output_shape is not EnumDelegationOutputShape.PLAIN_TEXT:
                raise ValueError("final_paragraph boundary requires plain_text shape")
            if self.markers:
                raise ValueError("final_paragraph boundary must not declare markers")
            if self.plain_text_constraints is None:
                raise ValueError("final_paragraph boundary requires word constraints")
        if any(not marker or marker != marker.strip() for marker in self.markers):
            raise ValueError("markers must be non-empty trimmed strings")
        if (
            self.render_start_marker is not None
            and self.render_start_marker not in self.markers
        ):
            raise ValueError("render_start_marker must be one declared marker")
        return self


class ModelDeliverableExtraction(BaseModel):
    """Exact extraction evidence. A refused result never carries raw content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deliverable: str
    preamble_chars: int = Field(ge=0)
    deliverable_start: int = Field(ge=0)
    deliverable_end: int = Field(ge=0)
    raw_chars: int = Field(ge=0)
    refusal: EnumDeliverableExtractionRefusal | None
    contract_failure_reasons: tuple[str, ...]


def extract_deliverable(
    raw_content: str,
    contract: ModelDeliverableContract,
    *,
    requested_shape: EnumRequestedResponseShape = EnumRequestedResponseShape.UNCONSTRAINED,
) -> ModelDeliverableExtraction:
    """Return only a contract-located deliverable, or a typed refusal.

    JSON has a schema-defined boundary: the last JSON value which conforms to
    the declared schema. Markdown and plain text deliberately have no such
    structural proof, so they require a declared marker. This function never
    guesses from prose and never returns raw content after a refusal.
    """
    if contract.output_shape is EnumDelegationOutputShape.JSON:
        located, contract_failure_reasons = _last_schema_conforming_json(
            raw_content, contract.json_schema
        )
        if located is None:
            return _refusal(
                raw_content,
                EnumDeliverableExtractionRefusal.NO_SCHEMA_CONFORMING_JSON,
                contract_failure_reasons,
            )
        start, end = located
    else:
        if contract.boundary_mode is EnumDeliverableBoundaryMode.FINAL_PARAGRAPH:
            paragraph_span = _final_paragraph_span(raw_content)
            if paragraph_span is None:
                return _refusal(
                    raw_content,
                    EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED,
                    (),
                )
            start, end = paragraph_span
        else:
            marker_span = _last_marker_span(raw_content, contract.markers)
            if marker_span is None:
                if requested_shape in (
                    EnumRequestedResponseShape.EXACT_LITERAL,
                    EnumRequestedResponseShape.SINGLE_WORD,
                ):
                    stripped = raw_content.strip()
                    if (
                        stripped
                        and "\n" not in stripped
                        and (
                            requested_shape
                            is not EnumRequestedResponseShape.SINGLE_WORD
                            or " " not in stripped
                        )
                    ):
                        start = raw_content.index(stripped)
                        end = start + len(stripped)
                        return ModelDeliverableExtraction(
                            deliverable=stripped,
                            preamble_chars=start,
                            deliverable_start=start,
                            deliverable_end=end,
                            raw_chars=len(raw_content),
                            refusal=None,
                            contract_failure_reasons=(),
                        )
                return _refusal(
                    raw_content,
                    EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED,
                    (),
                )
            marker_start, marker_end = marker_span
            if (
                contract.output_shape is EnumDelegationOutputShape.MARKDOWN
                and raw_content[marker_start:marker_end].strip()
                != contract.render_start_marker
            ):
                start = marker_start
            else:
                start = marker_end
                while start < len(raw_content) and raw_content[start].isspace():
                    start += 1
            end = len(raw_content)

    deliverable = raw_content[start:end]
    if contract.plain_text_constraints is not None:
        words = len(re.findall(r"\S+", deliverable))
        if not (
            contract.plain_text_constraints.min_words
            <= words
            <= contract.plain_text_constraints.max_words
        ):
            return _refusal(
                raw_content,
                EnumDeliverableExtractionRefusal.PLAIN_TEXT_CONSTRAINT_VIOLATION,
                (),
            )
    share = len(deliverable) / len(raw_content) if raw_content else 0.0
    refusal = (
        EnumDeliverableExtractionRefusal.BELOW_SHARE_FLOOR
        if share < contract.min_deliverable_share
        else None
    )
    return ModelDeliverableExtraction(
        deliverable=deliverable,
        preamble_chars=start,
        deliverable_start=start,
        deliverable_end=end,
        raw_chars=len(raw_content),
        refusal=refusal,
        contract_failure_reasons=(),
    )


def _last_schema_conforming_json(
    raw_content: str, schema: dict[str, object] | None
) -> tuple[tuple[int, int] | None, tuple[str, ...]]:
    assert schema is not None
    validator_class = jsonschema.validators.validator_for(schema)
    validator_class.check_schema(schema)
    decoder = json.JSONDecoder()
    last: tuple[int, int] | None = None
    last_failure_reasons: tuple[str, ...] = ()
    start = 0
    while start < len(raw_content):
        character = raw_content[start]
        if character not in '{["-0123456789tfn':
            start += 1
            continue
        try:
            candidate, end = decoder.raw_decode(raw_content, start)
        except json.JSONDecodeError:
            start += 1
            continue
        failure_reasons = tuple(schema_violation_reasons(candidate, schema))
        if not failure_reasons:
            last = (start, end)
        else:
            last_failure_reasons = failure_reasons
        # Resume scanning AFTER this candidate's span rather than at the next
        # character. A number embedded inside an already-decoded object (e.g.
        # the ``0.91`` in ``{"score": 0.91}``) is itself a valid, independently
        # parseable JSON value at its own start position; scanning byte-by-byte
        # revisits it as a second, later candidate whose scalar-vs-object
        # mismatch ("0.91 is not of type 'object'") then overwrites the
        # object's own, far more useful violation reasons ("'verdict' is a
        # required property") purely because it was seen last. Skipping past
        # `end` keeps every candidate a genuinely separate top-level value.
        start = end
    return last, last_failure_reasons


def resolve_deliverable_contract(
    response_contract: dict[str, object],
) -> ModelDeliverableContract:
    """Build an extractor contract from a declared response contract.

    A JSON Schema inherently declares JSON. Markdown and plain text must name
    ``x-omninode-output-shape`` because prompt prose cannot safely establish a
    boundary. Marker and floor values come from Market's authority file.
    """
    authority = resolve_delegation_output_authority()
    raw_shape = response_contract.get("x-omninode-output-shape")
    if raw_shape is None:
        output_shape = EnumDelegationOutputShape.JSON
    else:
        try:
            output_shape = EnumDelegationOutputShape(str(raw_shape))
        except ValueError as exc:
            raise ValueError(f"unknown declared output shape: {raw_shape}") from exc
    schema = {
        key: value
        for key, value in response_contract.items()
        if key != "x-omninode-output-shape"
    }
    if output_shape is EnumDelegationOutputShape.JSON:
        return ModelDeliverableContract(
            output_shape=output_shape,
            min_deliverable_share=authority.min_deliverable_share,
            json_schema=schema,
        )
    return ModelDeliverableContract(
        output_shape=output_shape,
        min_deliverable_share=authority.min_deliverable_share,
        markers=authority.markers_for(output_shape),
        render_start_marker=authority.markers_for(output_shape)[0],
    )


def resolve_task_class_deliverable_contract(
    task_class: str,
    response_contract: dict[str, object] | None,
) -> ModelDeliverableContract:
    """Resolve the one effective contract used for rendering, extraction and gate."""
    if response_contract is not None:
        return resolve_deliverable_contract(response_contract)
    output_contract = resolve_task_class_output_contract(task_class)
    if output_contract.output_shape is EnumDelegationOutputShape.JSON:
        raise ValueError("default JSON output contracts require a caller schema")
    authority = resolve_delegation_output_authority()
    return ModelDeliverableContract(
        output_shape=output_contract.output_shape,
        min_deliverable_share=authority.min_deliverable_share,
        markers=authority.markers_for(output_contract.output_shape),
        render_start_marker=output_contract.start_marker,
    )


def canonical_deliverable_contract_sha256(contract: ModelDeliverableContract) -> str:
    """Return the stable receipt identity of one resolved output contract."""
    canonical_bytes = json.dumps(
        contract.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical_bytes).hexdigest()


def _last_marker_span(
    raw_content: str, markers: tuple[str, ...]
) -> tuple[int, int] | None:
    offset = 0
    last: tuple[int, int] | None = None
    for line in raw_content.splitlines(keepends=True):
        if line.strip() in markers:
            last = (offset, offset + len(line))
        offset += len(line)
    return last


def _final_paragraph_span(raw_content: str) -> tuple[int, int] | None:
    """Locate only the final blank-line-separated paragraph when declared."""
    paragraphs = list(re.finditer(r"[^\n](?:.*?)(?=\n[ \t]*\n|\Z)", raw_content, re.S))
    if not paragraphs:
        return None
    paragraph = paragraphs[-1]
    return paragraph.span()


def _refusal(
    raw_content: str,
    reason: EnumDeliverableExtractionRefusal,
    contract_failure_reasons: tuple[str, ...],
) -> ModelDeliverableExtraction:
    return ModelDeliverableExtraction(
        deliverable="",
        preamble_chars=len(raw_content),
        deliverable_start=len(raw_content),
        deliverable_end=len(raw_content),
        raw_chars=len(raw_content),
        refusal=reason,
        contract_failure_reasons=contract_failure_reasons,
    )


__all__ = [
    "EnumDeliverableBoundaryMode",
    "EnumDeliverableExtractionRefusal",
    "ModelDeliverableContract",
    "ModelDeliverableExtraction",
    "ModelPlainTextOutputConstraints",
    "canonical_deliverable_contract_sha256",
    "extract_deliverable",
    "resolve_deliverable_contract",
    "resolve_task_class_deliverable_contract",
]
