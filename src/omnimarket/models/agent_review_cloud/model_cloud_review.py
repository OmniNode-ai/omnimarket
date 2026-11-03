# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unsigned review a review-only cloud session returns, and its strict parse.

Shared by the cloud placement node (which reads the ended session's final message and
decides how the lane closes) and the agent review cloud rung (which renders the
review-only brief and turns the parsed review into one voter's vote), so neither node
imports the other's package.

The session writes one review in the v2 compact verdict format of the Hostile Reviewer
(OMN-20910; ``{"v", "f": [{"c", "p", "l", "s", "y", "n"}], "more", "done"}``) after a
marker line naming the head it reviewed. This module keeps its own strict view of that
wire shape, as the response parser does, and never salvages a reply that does not fit:
a missing marker, a head that is not the one requested, JSON that does not decode, a
field the shape does not have (a signature included), ``more: true`` or ``done``
absent is a refusal with a typed reason, never a shorter review.

The review is unsigned by construction: the dispatch node signs the receipt that counts
it. A session never holds a signing key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REVIEW_ONLY_KIND = "review-only"
"""The ``Lane kind:`` of a review-only lane: it reviews a head and leaves no pull request."""

REVIEW_MARKER = "ONEX-AGENT-REVIEW"
"""The line ``ONEX-AGENT-REVIEW head=<40 hex>`` that opens the review in the final message."""

REVIEW_HEAD_PREFIX = "Review head:"
"""The brief's column-zero line naming the exact head the session must review."""

REVIEW_FILE = "/tmp/onex-agent-review.txt"
"""Where the session also writes the review, outside its checkout, so it is never pushed."""

FINAL_TEXT_MAX = 200_000
"""The longest final message the parse reads; a longer one is refused, never cut."""

_SHA = re.compile(r"[0-9a-f]{40}")
_MARKER_LINE = re.compile(rf"^{REVIEW_MARKER} head=(\S*)[ \t]*$", re.MULTILINE)
_HEAD_LINE = re.compile(rf"{re.escape(REVIEW_HEAD_PREFIX)}[ \t]*(\S*)[ \t]*")
_FENCE = re.compile(r"^[ \t]*(```|~~~)[A-Za-z0-9_-]*[ \t]*$", re.MULTILINE)

SEVERITY_WORDS: dict[str, str] = {
    "C": "critical",
    "M": "major",
    "m": "minor",
    "n": "nit",
}
"""The compact severity letters and the words the voter review carries."""


class EnumCloudReviewRefusal(StrEnum):
    """Why a session's text is not a review the lane may return."""

    NO_TEXT = "no_text"
    TOO_LONG = "too_long"
    NO_MARKER = "no_marker"
    BAD_HEAD = "bad_head"
    HEAD_DIFFERS = "head_differs"
    NO_JSON = "no_json"
    SCHEMA = "schema"
    MORE = "more"


class ModelCompactReviewFinding(BaseModel):
    """One compact finding: ``{"c":"T01","p":"src/x.py","l":42,"s":"M"}``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    c: str = Field(min_length=1)
    p: str = Field(min_length=1)
    line: int = Field(alias="l", ge=0)
    s: Literal["C", "M", "m", "n"]
    y: (
        Literal[
            "architecture",
            "security",
            "performance",
            "correctness",
            "completeness",
            "feasibility",
            "testing",
            "style",
        ]
        | None
    ) = None
    n: str | None = Field(default=None, max_length=120)


class ModelCompactReviewVerdict(BaseModel):
    """A complete compact verdict; ``done`` is the terminal marker a cut reply lacks."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    v: str = Field(min_length=1)
    f: list[ModelCompactReviewFinding]
    more: bool
    done: Literal[True]


class ModelCloudReviewFinding(BaseModel):
    """One finding in the shape the dispatch node's voter review carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: str = Field(min_length=1)
    claim: str = ""


class ModelCloudReview(BaseModel):
    """The typed unsigned review read back from one ended session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    catalog_version: str = Field(min_length=1)
    findings: tuple[ModelCloudReviewFinding, ...] = ()
    source: Literal["final_message", "review_file"]


class ModelCloudReviewRefused(BaseModel):
    """A text that is not a review, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumCloudReviewRefusal
    detail: str = ""


def _column_zero(text: str) -> Iterator[str]:
    """Lines outside fenced blocks, so a quoted example is never read as metadata."""
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = marker if fence is None else (None if fence == marker else fence)
            continue
        if fence is None:
            yield line


def review_head(brief: str) -> str | None:
    """The head the brief asks the session to review (its first column-zero ``Review head:`` line), or None."""
    for line in _column_zero(brief):
        found = _HEAD_LINE.fullmatch(line)
        if found:
            head = found.group(1).lower()
            return head if _SHA.fullmatch(head) else None
    return None


def _refused(
    reason: EnumCloudReviewRefusal, detail: str = ""
) -> ModelCloudReviewRefused:
    return ModelCloudReviewRefused(reason=reason, detail=detail)


def parse_cloud_review(
    text: str,
    expected_head: str | None,
    source: Literal["final_message", "review_file"] = "final_message",
) -> ModelCloudReview | ModelCloudReviewRefused:
    """The review the text holds after its last marker line, or the typed reason it holds none.

    Error details name the rule that failed and never echo the reply's text."""
    if not text.strip():
        return _refused(EnumCloudReviewRefusal.NO_TEXT)
    if len(text) > FINAL_TEXT_MAX:
        return _refused(EnumCloudReviewRefusal.TOO_LONG, f"over {FINAL_TEXT_MAX} chars")
    markers = list(_MARKER_LINE.finditer(text))
    if not markers:
        return _refused(EnumCloudReviewRefusal.NO_MARKER, f"no {REVIEW_MARKER} line")
    last = markers[-1]
    head = last.group(1).lower()
    if not _SHA.fullmatch(head):
        return _refused(EnumCloudReviewRefusal.BAD_HEAD, "head is not 40 hex")
    if expected_head is not None and head != expected_head.lower():
        return _refused(
            EnumCloudReviewRefusal.HEAD_DIFFERS, "the review names another head"
        )
    body = _FENCE.sub("", text[last.end() :]).strip()
    try:
        payload, _end = json.JSONDecoder().raw_decode(body)
    except ValueError:
        return _refused(
            EnumCloudReviewRefusal.NO_JSON, "no JSON object after the marker"
        )
    try:
        verdict = ModelCompactReviewVerdict.model_validate(payload)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in err["loc"]) or "<document>"
                for err in exc.errors(include_input=False, include_context=False)
            }
        )
        return _refused(EnumCloudReviewRefusal.SCHEMA, f"fields {fields}")
    if verdict.more:
        return _refused(
            EnumCloudReviewRefusal.MORE, "the reviewer had more findings than it listed"
        )
    return ModelCloudReview(
        head_sha=head,
        catalog_version=verdict.v,
        findings=tuple(
            ModelCloudReviewFinding(
                path=item.p,
                line=item.line,
                severity=SEVERITY_WORDS[item.s],
                claim=" ".join(
                    part for part in (item.c, item.y or "", item.n or "") if part
                ),
            )
            for item in verdict.f
        ),
        source=source,
    )


def read_session_review(
    final_text: str, file_text: str, expected_head: str | None
) -> ModelCloudReview | ModelCloudReviewRefused:
    """The review of the final message, else of the review file; the final message's refusal when neither holds one."""
    first = parse_cloud_review(final_text, expected_head, "final_message")
    if isinstance(first, ModelCloudReview) or not file_text.strip():
        return first
    second = parse_cloud_review(file_text, expected_head, "review_file")
    return second if isinstance(second, ModelCloudReview) else first


__all__ = [
    "FINAL_TEXT_MAX",
    "REVIEW_FILE",
    "REVIEW_HEAD_PREFIX",
    "REVIEW_MARKER",
    "REVIEW_ONLY_KIND",
    "SEVERITY_WORDS",
    "EnumCloudReviewRefusal",
    "ModelCloudReview",
    "ModelCloudReviewFinding",
    "ModelCloudReviewRefused",
    "ModelCompactReviewFinding",
    "ModelCompactReviewVerdict",
    "parse_cloud_review",
    "read_session_review",
    "review_head",
]
