# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public wire types for error producers, normalization and replay facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelErrorSubject(BaseModel):
    """Read-back and dispatch facts; deliberately excluded from class identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    repository: str = ""
    pr: str = ""
    head: str = ""
    host: str = ""
    lane: str = ""
    ticket: str = ""
    time: str = ""


class ModelErrorEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    code: str | None = None
    text: str
    text_kind: Literal["machine", "prose"] = "machine"
    subject: ModelErrorSubject = Field(default_factory=ModelErrorSubject)


class ModelErrorStripStage(BaseModel):
    """An ordered regex or input-host/lane-pattern substitution."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    kind: Literal["regex", "hosts", "lanes"] = "regex"
    pattern: str = ""
    replacement: str


class ModelErrorProducerAdapter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    producer: str
    unique_codes: bool


class ModelErrorNormalizerConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    strip_stages: tuple[ModelErrorStripStage, ...] = Field(min_length=1)
    producer_adapters: tuple[ModelErrorProducerAdapter, ...] = Field(min_length=1)
    lane_name_patterns: tuple[str, ...]
    max_open_triage: int = Field(gt=0)


class ModelErrorNormalizerFacts(BaseModel):
    """Snapshot the configuration AND host registry for reproducible old keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    configuration: ModelErrorNormalizerConfiguration
    hosts: tuple[str, ...]


class ModelErrorClassAlias(BaseModel):
    """A triage row resolves a provisional key, or explicitly records none."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    provisional_key: str
    canonical_key: str | None


class ModelErrorClassNormalizeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event: ModelErrorEvent
    facts: ModelErrorNormalizerFacts
    aliases: tuple[ModelErrorClassAlias, ...] = ()
    key_version: str | None = Field(
        default=None,
        description="Replay pin: refuse when these facts do not hash to this version.",
    )


class ModelErrorClassNormalizeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    class_key: str
    key_version: str
    template: str
    slug: str
    provisional: bool
    identity: str
    canonical_key: str | None
    alias_state: Literal["not_provisional", "unresolved", "canonical", "none"]


__all__ = [
    "ModelErrorClassAlias",
    "ModelErrorClassNormalizeRequest",
    "ModelErrorClassNormalizeResult",
    "ModelErrorEvent",
    "ModelErrorNormalizerConfiguration",
    "ModelErrorNormalizerFacts",
    "ModelErrorProducerAdapter",
    "ModelErrorStripStage",
    "ModelErrorSubject",
]
