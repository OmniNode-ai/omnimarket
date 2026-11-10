# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_pr_landing_liveness_read_effect: the reader, the checkout and the timeouts, none in code (OMN-23529).

Found at ``<root>/node_pr_landing_liveness_read_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load and the
refusal names the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_pr_landing_liveness_read_effect"


class EnumLivenessReaderBackend(StrEnum):
    INMEMORY = "inmemory"
    COMMAND = "command"


class ModelLivenessReader(BaseModel):
    """Which reader runs: the register completeness command, or a read recorded in memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumLivenessReaderBackend
    argv: tuple[str, ...] = Field(min_length=1)


class ModelLivenessCheckout(BaseModel):
    """Where the register and the PR head checkout are, by environment variable, and the base the head is compared to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    internal_root_env: str = Field(min_length=1)
    registry_root_env: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)


class ModelLivenessTimeouts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    read_seconds: float = Field(gt=0)
    git_seconds: float = Field(gt=0)


class ModelLivenessTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    completed: str = Field(min_length=1)
    failed: str = Field(min_length=1)


class ModelLivenessReadOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: ModelLivenessReader
    checkout: ModelLivenessCheckout
    timeouts: ModelLivenessTimeouts
    topics: ModelLivenessTopics


def load_overlay(
    environ: Mapping[str, str] | None = None, *, explicit: object | None = None
) -> ModelLivenessReadOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelLivenessReadOverlay, environ, explicit=explicit
    )


__all__ = [
    "NODE_NAME",
    "EnumLivenessReaderBackend",
    "ModelLivenessCheckout",
    "ModelLivenessReadOverlay",
    "ModelLivenessReader",
    "ModelLivenessTimeouts",
    "ModelLivenessTopics",
    "load_overlay",
]
