# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit external stack inputs; setup never supplies a default topology."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelSetupLocalStackSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    compose_file: Path
    topology_file: Path
    max_wait_seconds: int = Field(default=120, ge=1, le=1800)

    @field_validator("compose_file", "topology_file")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value.expanduser().absolute()
