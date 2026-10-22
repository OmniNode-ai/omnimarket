# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated input for local access-mode invocation."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelLocalAccessModeRequest(BaseModel):
    """Session facts supplied by a local hook or CLI caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    state_dir: Path
    environ: dict[str, str] = Field(default_factory=dict)
    transcript_entrypoint: str = ""
