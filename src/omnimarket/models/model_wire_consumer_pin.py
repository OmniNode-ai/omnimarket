# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the consumer-pin reader behind the Wire Compatibility Gate (OMN-21901)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelConsumerPinFiles(BaseModel):
    """One consumer's own pin files, as the gate was handed them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    pyproject_path: str | None = None
    lock_path: str | None = None


class ModelWireConsumerPinRequest(BaseModel):
    """The consumers to read and the consumers that must be among them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(min_length=1)
    consumers: tuple[ModelConsumerPinFiles, ...] = ()
    required: tuple[str, ...] = ()


class ModelPinBaseline(BaseModel):
    """One distinct dev-merge tag that at least one consumer pins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    commit: str
    consumers: tuple[str, ...]


class ModelWireConsumerPinResult(BaseModel):
    """Every distinct pinned tag, oldest first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baselines: tuple[ModelPinBaseline, ...]
