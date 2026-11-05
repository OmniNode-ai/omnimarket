# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the host bring-up overlay compute node.

A host overlay is per-host data: facts keyed by the symbolic check subject of the
declared bring-up target rows, each with the state it recorded and the evidence
it rests on. Every model is frozen and forbids extra keys. A fact with no
evidence, a blank evidence string, a state outside the enum or a subject that is
not a lowercase identifier fails validation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.host_bringup import EnumBringupCheckKind, EnumObservedState

_IDENTIFIER = r"^[a-z][a-z0-9_]*$"
_TARGET_NAME = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"


class EnumOverlayStatus(StrEnum):
    RESOLVED = "resolved"
    REFUSED = "refused"


class EnumOverlayRefusalKind(StrEnum):
    UNKNOWN_SUBJECT = "unknown_subject"
    DUPLICATE_TARGET = "duplicate_target"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelOverlayCheckRow(_Frozen):
    """The check part of one declared target row: its name, check kind and symbolic subject."""

    target: str = Field(pattern=_TARGET_NAME)
    kind: EnumBringupCheckKind
    subject: str = Field(pattern=_IDENTIFIER)


class ModelHostFact(_Frozen):
    """One recorded reading of a check subject on the host."""

    subject: str = Field(pattern=_IDENTIFIER)
    state: EnumObservedState
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _evidence_not_blank(self) -> Self:
        if not self.evidence.strip():
            raise ValueError(f"fact {self.subject}: evidence is blank")
        return self


class ModelHostOverlay(_Frozen):
    """The overlay data of one host."""

    host: str = Field(pattern=_IDENTIFIER)
    facts: tuple[ModelHostFact, ...] = ()


class ModelHostBringupOverlayRequest(_Frozen):
    checks: tuple[ModelOverlayCheckRow, ...] = Field(min_length=1)
    overlay: ModelHostOverlay


class ModelOverlayRefusal(_Frozen):
    kind: EnumOverlayRefusalKind
    names: tuple[str, ...]
    message: str


class ModelResolvedObservation(_Frozen):
    """One target's observed state, with the evidence of every fact it was resolved from."""

    target: str
    subject: str
    state: EnumObservedState
    evidence: tuple[str, ...] = ()


class ModelHostBringupOverlayResult(_Frozen):
    status: EnumOverlayStatus
    host: str
    refusals: tuple[ModelOverlayRefusal, ...] = ()
    observations: tuple[ModelResolvedObservation, ...] = ()


__all__ = [
    "EnumOverlayRefusalKind",
    "EnumOverlayStatus",
    "ModelHostBringupOverlayRequest",
    "ModelHostBringupOverlayResult",
    "ModelHostFact",
    "ModelHostOverlay",
    "ModelOverlayCheckRow",
    "ModelOverlayRefusal",
    "ModelResolvedObservation",
]
