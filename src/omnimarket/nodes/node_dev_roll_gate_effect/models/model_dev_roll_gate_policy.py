# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of node_dev_roll_gate_effect.

The window, the minimum start interval and the health bounds are the batching node's
overlay values (node_dev_roll_request_compute); the gate carries only what it needs
to hold, release and settle. There is no packaged fallback.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDevRollGateInMemoryStore(BaseModel):
    """The in-memory store configuration (tests, never a deployment)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory"]


class ModelDevRollGateFileStore(BaseModel):
    """The durable file store configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["file"]
    home_env: str = Field(min_length=1)
    relpath: str = Field(min_length=1)


class ModelDevRollGatePolicy(BaseModel):
    """The overlay document of node_dev_roll_gate_effect.

    ``off`` releases whatever is held at the next input and holds nothing new.
    ``live`` holds every forwarded command until the batching decision rolls it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["off", "live"]
    issuer_repository_pattern: str = Field(min_length=1)
    unmatched_repository: str = Field(min_length=1)
    roll_unfinished_after_seconds: int = Field(gt=0)
    ids_kept: int = Field(gt=0)
    store: Annotated[
        ModelDevRollGateInMemoryStore | ModelDevRollGateFileStore,
        Field(discriminator="backend"),
    ]

    @model_validator(mode="after")
    def _issuer_pattern_naming_a_repository(self) -> ModelDevRollGatePolicy:
        try:
            compiled = re.compile(self.issuer_repository_pattern)
        except re.error as exc:
            raise ValueError(
                f"issuer_repository_pattern is not a regular expression ({exc})"
            ) from exc
        if "repository" not in compiled.groupindex:
            raise ValueError(
                "issuer_repository_pattern must carry a named group 'repository'"
            )
        return self
