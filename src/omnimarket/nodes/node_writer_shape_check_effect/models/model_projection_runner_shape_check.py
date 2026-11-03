# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, finding and result of the projection runner shape check (OMN-18883)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelProjectionRunnerShapeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    nodes_root: str = Field(
        description="Directory holding the node_* packages to scan (src/omnimarket/nodes)."
    )
    package_root: str = Field(
        description=(
            "Directory the dotted handler modules resolve against "
            "(src); the frozen runner set is keyed by dotted module."
        )
    )
    frozen_runners: tuple[str, ...] = Field(
        description="Frozen, shrink-only 'module:Class' set of runner subclasses."
    )
    dedicated_deployments: tuple[str, ...] = Field(
        description=(
            "Frozen 'module:Class' set of runner subclasses owned by a dedicated "
            "writer Deployment instead of in-process dispatch."
        )
    )
    min_runners: int = Field(
        default=1,
        description="Vacuity floor: fewer runners seen than this is an error.",
    )


class ModelProjectionRunnerShapeFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule: str = Field(
        description="typed-fold-on-writer-path, undeclared-dispatch, runner-baseline-growth, runner-baseline-stale or scan-vacuous."
    )
    subject: str = Field(description="node, or module:Class, the finding is about.")
    message: str


class ModelProjectionRunnerShapeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    runners_seen: int
    findings: tuple[ModelProjectionRunnerShapeFinding, ...]
