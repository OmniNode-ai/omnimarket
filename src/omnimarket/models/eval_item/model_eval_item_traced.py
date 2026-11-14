# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The full trace of one scoring, and its split into numbered parts (OMN-24170).

A trace is everything the model was given and answered and every check's whole
output. A sha256 beside a text only proves it is the text scored, so the trace
itself is kept: whole, never cut. Over the broker's message bound it goes as
numbered parts that reassemble byte for byte to the trace's sha256, and a set of
parts that does not is refused.

The bound is ``ModelEvalTraceBound``, the same shape as
``ModelWorkLedgerAnswerBound`` with one more step: a part's bytes travel base64
encoded (a cut can land inside a multi-byte character), so the encoded part plus
the envelope headroom must fit under the broker's limit.

Pure models and pure functions: no clock, no I/O.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelEvalCheckTrace(BaseModel):
    """One check run: its command, exit code and whole output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    output: str


class ModelEvalItemTrace(BaseModel):
    """What one scoring was given and answered, whole.

    ``event_id`` and ``scoring_id`` ride on the parts, not here: ``scoring_id`` is
    derived from this trace's sha256, so the body cannot hold it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_text: str
    generation_settings: tuple[tuple[str, str], ...] = ()
    response_text: str = ""
    diff_text: str = ""
    checks: tuple[ModelEvalCheckTrace, ...] = ()

    def canonical_bytes(self) -> bytes:
        """The UTF-8 JSON bytes whose sha256 names this trace."""
        return self.model_dump_json().encode("utf-8")

    def sha256(self) -> str:
        """Hex sha256 of ``canonical_bytes``."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ModelEvalTraceBound(BaseModel):
    """The size a trace part stays under; validated so the contract does not load otherwise."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_max_request_size: int = Field(gt=0)
    envelope_headroom_bytes: int = Field(ge=0)
    max_part_bytes: int = Field(gt=0, description="Raw bytes of trace in one part.")

    @property
    def encoded_part_bytes(self) -> int:
        """Base64 length of a full part."""
        return -(-self.max_part_bytes // 3) * 4

    @model_validator(mode="after")
    def _fits_under_the_broker(self) -> ModelEvalTraceBound:
        needed = self.encoded_part_bytes + self.envelope_headroom_bytes
        if needed > self.broker_max_request_size:
            raise ValueError(
                f"encoded part {self.encoded_part_bytes} plus envelope_headroom_bytes "
                f"{self.envelope_headroom_bytes} exceeds broker_max_request_size "
                f"{self.broker_max_request_size}"
            )
        return self


DEFAULT_TRACE_BOUND = ModelEvalTraceBound(
    broker_max_request_size=1048588,
    envelope_headroom_bytes=131072,
    max_part_bytes=688128,
)
"""The work ledger answer bound's broker limit and headroom; 688128 raw bytes encode to 917504."""


class ModelEvalItemTracePart(BaseModel):
    """One numbered part of a trace, as it goes on the trace topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    scoring_id: UUID
    trace_sha256: str = Field(min_length=64, max_length=64)
    part_index: int = Field(ge=0)
    part_count: int = Field(ge=1)
    chunk_b64: str
    chunk_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _part_is_what_it_says(self) -> ModelEvalItemTracePart:
        if self.part_index >= self.part_count:
            raise ValueError(
                f"part_index {self.part_index} is not below part_count {self.part_count}"
            )
        try:
            raw = base64.b64decode(self.chunk_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"chunk_b64 is not base64: {exc}") from exc
        if hashlib.sha256(raw).hexdigest() != self.chunk_sha256:
            raise ValueError("chunk_sha256 does not match the part's bytes")
        return self


def split_trace(
    trace: ModelEvalItemTrace,
    *,
    event_id: UUID,
    scoring_id: UUID,
    bound: ModelEvalTraceBound = DEFAULT_TRACE_BOUND,
) -> tuple[ModelEvalItemTracePart, ...]:
    """Split a trace into numbered parts of at most ``bound.max_part_bytes`` bytes; nothing is cut."""
    body = trace.canonical_bytes()
    digest = hashlib.sha256(body).hexdigest()
    step = bound.max_part_bytes
    chunks = [body[i : i + step] for i in range(0, len(body), step)] or [b""]
    return tuple(
        ModelEvalItemTracePart(
            event_id=event_id,
            scoring_id=scoring_id,
            trace_sha256=digest,
            part_index=index,
            part_count=len(chunks),
            chunk_b64=base64.b64encode(chunk).decode("ascii"),
            chunk_sha256=hashlib.sha256(chunk).hexdigest(),
        )
        for index, chunk in enumerate(chunks)
    )


def assemble_trace(parts: tuple[ModelEvalItemTracePart, ...]) -> ModelEvalItemTrace:
    """Reassemble parts in any order; refuse a missing, duplicated, mixed or altered set."""
    if not parts:
        raise ValueError("no parts to assemble")
    first = parts[0]
    for part in parts:
        for name in ("event_id", "scoring_id", "trace_sha256", "part_count"):
            if getattr(part, name) != getattr(first, name):
                raise ValueError(
                    f"parts disagree on {name}: {getattr(first, name)} vs {getattr(part, name)}"
                )
    indexes = [part.part_index for part in parts]
    if len(set(indexes)) != len(indexes):
        raise ValueError(f"duplicate part index in {sorted(indexes)}")
    missing = sorted(set(range(first.part_count)) - set(indexes))
    if missing:
        raise ValueError(f"missing parts {missing} of {first.part_count}")
    body = b"".join(
        base64.b64decode(part.chunk_b64)
        for part in sorted(parts, key=lambda part: part.part_index)
    )
    if hashlib.sha256(body).hexdigest() != first.trace_sha256:
        raise ValueError(
            "reassembled trace does not match its sha256 "
            f"{first.trace_sha256}: the parts were altered"
        )
    return ModelEvalItemTrace.model_validate_json(body)


__all__ = [
    "DEFAULT_TRACE_BOUND",
    "ModelEvalCheckTrace",
    "ModelEvalItemTrace",
    "ModelEvalItemTracePart",
    "ModelEvalTraceBound",
    "assemble_trace",
    "split_trace",
]
