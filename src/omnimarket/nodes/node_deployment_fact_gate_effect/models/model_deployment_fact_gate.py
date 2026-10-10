# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the deployment-fact gate (OMN-20287)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind
from omnimarket.models.delegation.model_deployment_fact_marker import (
    ModelDeploymentFactOccurrence,
)


class ModelDeploymentFactGateRequest(BaseModel):
    """Which tree to judge, and against which base revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(..., min_length=1, description="The repository checkout.")
    base_ref: str | None = Field(
        default="HEAD",
        description=(
            "The revision whose packaged configs are the shrink-only baseline. "
            "None judges the tree against no baseline, which reports every "
            "non-neutral value the packaged configs carry."
        ),
    )


class ModelPackagedConfigTexts(BaseModel):
    """One packaged config file's text at the base revision and in the tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_name: str = Field(..., min_length=1)
    base_text: str | None = Field(
        default=None, description="None when the base revision has no such file."
    )
    head_text: str


class ModelNewDeploymentFact(BaseModel):
    """A non-neutral value in a marked field that the base revision did not carry there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_name: str
    path: str
    kind: EnumDeploymentFactKind
    value: str
    base_count: int = Field(..., ge=0)
    head_count: int = Field(..., ge=1)


class ModelNewUndeclaredKey(BaseModel):
    """A key a packaged config carries that its typed model does not declare."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_name: str
    path: str


class ModelDeploymentFactGateResult(BaseModel):
    """What the gate found. It passes only when both finding lists are empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_ref: str | None
    new_facts: tuple[ModelNewDeploymentFact, ...] = ()
    new_undeclared_keys: tuple[ModelNewUndeclaredKey, ...] = ()
    inventory: tuple[ModelDeploymentFactOccurrence, ...] = Field(
        default=(),
        description=(
            "Every non-neutral value the tree's packaged configs carry in a "
            "marked field: the deployment facts still to move to an overlay."
        ),
    )

    @property
    def passed(self) -> bool:
        return not self.new_facts and not self.new_undeclared_keys


__all__: list[str] = [
    "ModelDeploymentFactGateRequest",
    "ModelDeploymentFactGateResult",
    "ModelNewDeploymentFact",
    "ModelNewUndeclaredKey",
    "ModelPackagedConfigTexts",
]
