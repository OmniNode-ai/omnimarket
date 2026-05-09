"""Result model for context bundle generation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_context_bundle_generator_compute.models.model_context_bundle import (
    ModelContextBundleL0,
    ModelContextBundleL1,
    ModelContextBundleL2,
    ModelContextBundleL3,
    ModelContextBundleL4,
)

BundleUnion = Annotated[
    ModelContextBundleL0
    | ModelContextBundleL1
    | ModelContextBundleL2
    | ModelContextBundleL3
    | ModelContextBundleL4,
    ...,
]


class EnumBundleStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelContextBundleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumBundleStatus
    bundle_id: str
    requested_level: str
    achieved_level: str
    bundle: BundleUnion
    error: str | None = None


__all__ = ["BundleUnion", "EnumBundleStatus", "ModelContextBundleResult"]
