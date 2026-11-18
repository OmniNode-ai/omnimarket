# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of node_dev_roll_failure_capture_effect."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDevRollInMemoryStore(BaseModel):
    """The in-memory artifact store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory"]


class ModelDevRollFileStore(BaseModel):
    """The file artifact store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["file"]
    home_env: str = Field(min_length=1)
    relpath: str = Field(min_length=1)


class ModelDevRollCapturePolicy(BaseModel):
    """The overlay document of node_dev_roll_failure_capture_effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store: Annotated[
        ModelDevRollInMemoryStore | ModelDevRollFileStore,
        Field(discriminator="backend"),
    ]
    max_log_bytes: int = Field(ge=1)
