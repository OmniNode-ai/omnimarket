# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Files prepared for one tools-only morning phase session."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from ..tools.context import ModelMorningToolContext


class ModelMorningPhaseSessionFilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    context: ModelMorningToolContext
    schema_definition: dict[str, JsonValue]
    state_root: Path | None = None


class ModelMorningPhaseSessionFiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: Path
    config_file: Path
    schema_file: Path
    argv: list[str]
