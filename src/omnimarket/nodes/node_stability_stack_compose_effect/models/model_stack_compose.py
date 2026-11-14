# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the stability stack compose effect node (OMN-23968).

Every model is frozen and forbids extra keys. The step types come from the
plan compute node's models; this module only adds the effect's request,
output and refusal vocabulary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    ModelStackStep,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCommandOutput(_Frozen):
    """What one executed command produced."""

    returncode: int
    stdout: str
    stderr: str


class ModelStackStepRequest(_Frozen):
    """Which declared stack to compose, and the one step to execute."""

    correlation_id: UUID
    stack: str = Field(min_length=1)
    step: ModelStackStep


class EnumStackStepOutcome(StrEnum):
    """How the one step ended."""

    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"


class EnumStackRefusalReason(StrEnum):
    """Why the effect refused to run anything."""

    UNKNOWN_STACK = "unknown_stack"
    HOST_NOT_LOCAL = "host_not_local"
    SOURCE_DIR_UNSET = "source_dir_unset"
    NOT_RENDERED = "not_rendered"
    DECLARED_NAME_ABSENT = "declared_name_absent"
    UNKNOWN_STEP = "unknown_step"


class ModelStackStepResult(_Frozen):
    """The outcome of one step: what ran, what was read and what was produced.

    ``reason`` names the refusal (empty unless ``outcome`` is ``refused``);
    ``reading`` is plain text about what was read; ``digest`` is the sha256 hex
    of the rendered compose texts, filled only for ``render`` (from the texts
    written) and for every other step that ran (read back from disk).
    """

    correlation_id: UUID
    stack: str = Field(min_length=1)
    step_kind: str
    outcome: EnumStackStepOutcome
    reason: EnumStackRefusalReason | str = ""
    reading: str = ""
    digest: str = ""
    items: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.outcome == EnumStackStepOutcome.REFUSED and not self.reason:
            raise ValueError("a refused result must name its refusal reason")
        return self


__all__ = [
    "EnumStackRefusalReason",
    "EnumStackStepOutcome",
    "ModelCommandOutput",
    "ModelStackStepRequest",
    "ModelStackStepResult",
]
