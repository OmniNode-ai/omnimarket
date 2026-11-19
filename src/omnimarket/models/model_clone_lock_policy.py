# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-owned persistent clone flock policy for the host reconcile effect."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

LockFileName = Annotated[str, StringConstraints(pattern=r"^[^/]+$")]


class ModelCloneLockOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    additional_persistent_flock_files: tuple[LockFileName, ...] = ()


class ModelCloneLockPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    persistent_flock_files: tuple[LockFileName, ...] = Field(min_length=1)
