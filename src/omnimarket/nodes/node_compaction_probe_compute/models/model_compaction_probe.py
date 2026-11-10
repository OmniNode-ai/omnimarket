# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the compaction probe node.

The input is a list of effective rulings, each with the row's own ``question=``
cell, the operator's quoted decision, the spans a recalled answer must carry and,
for an amending ruling, the text of the ruling it superseded. The derived set
that selects effective rulings from the ledger supplies these fields; this node
reads no ledger.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "EnumInversionSource",
    "EnumProbeAudit",
    "EnumProbeVerdict",
    "ModelBuiltContext",
    "ModelCompactionProbe",
    "ModelCompactionProbeRequest",
    "ModelCompactionProbeResult",
    "ModelControlContexts",
    "ModelEffectiveRuling",
    "ModelInvertedEntry",
    "ModelProbeJudgeRequest",
    "ModelProbeJudgeResult",
]


class EnumProbeAudit(StrEnum):
    """Leak audit outcome of one probe, decided before any arm runs."""

    ACCEPTED = "ACCEPTED"
    PROBE_LEAK = "PROBE_LEAK"


class EnumProbeVerdict(StrEnum):
    """Verdict on one answer to a ruling's probe."""

    RECALLED = "RECALLED"
    ABSENT = "ABSENT"
    CONTRADICTED = "CONTRADICTED"


class EnumInversionSource(StrEnum):
    """What the inverted context carries for one ruling."""

    PREDECESSOR = "PREDECESSOR"
    NEGATION = "NEGATION"


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class ModelEffectiveRuling(BaseModel):
    """One ruling in force at the measured boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str = Field(..., description="The ruling row's timestamp, its identity.")
    kind: str = Field(default="RULING", description="RULING or OPERATOR_CONSENT.")
    question: str = Field(..., description="The row's own question= cell.")
    quote: str = Field(..., description="The operator's quoted decision.")
    required_spans: tuple[str, ...] = Field(
        ..., min_length=1, description="Spans a recalled answer must carry."
    )
    predecessor_text: str | None = Field(
        default=None, description="Text of the ruling this one superseded, if any."
    )

    @field_validator("stamp", "kind", "question", "quote")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("required_spans")
    @classmethod
    def _spans(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for span in value:
            _not_blank(span)
        return value

    @field_validator("predecessor_text")
    @classmethod
    def _predecessor(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class ModelCompactionProbeRequest(BaseModel):
    """Effective rulings to build probes and control contexts for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    rulings: tuple[ModelEffectiveRuling, ...] = Field(..., min_length=1)

    @field_validator("correlation_id")
    @classmethod
    def _correlation(cls, value: str) -> str:
        return _not_blank(value)

    @model_validator(mode="after")
    def _unique_stamps(self) -> ModelCompactionProbeRequest:
        stamps = [ruling.stamp for ruling in self.rulings]
        if len(stamps) != len(set(stamps)):
            raise ValueError("ruling stamps must be unique")
        return self


class ModelCompactionProbe(BaseModel):
    """One probe and its leak audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    kind: str
    probe_text: str
    audit: EnumProbeAudit
    leaked_words: str | None = Field(
        default=None, description="The first shared run of words, when refused."
    )


class ModelBuiltContext(BaseModel):
    """One control context and its digest, for the answer key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    text: str
    sha256: str


class ModelInvertedEntry(BaseModel):
    """What the inverted context carries for one ruling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    source: EnumInversionSource
    text: str


class ModelControlContexts(BaseModel):
    """The oracle, empty and inverted contexts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    oracle: ModelBuiltContext
    empty: ModelBuiltContext
    inverted: ModelBuiltContext
    inverted_entries: tuple[ModelInvertedEntry, ...]


class ModelCompactionProbeResult(BaseModel):
    """Probes, the leak audit counts and the three control contexts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    probes: tuple[ModelCompactionProbe, ...]
    probes_total: int
    probes_accepted: int
    probes_refused_leak: int
    contexts: ModelControlContexts


class ModelProbeJudgeRequest(BaseModel):
    """One answer to a ruling's probe, for the deterministic judge fixture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str
    ruling: ModelEffectiveRuling


class ModelProbeJudgeResult(BaseModel):
    """The judge fixture's verdict on one answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    verdict: EnumProbeVerdict
    reason: str
