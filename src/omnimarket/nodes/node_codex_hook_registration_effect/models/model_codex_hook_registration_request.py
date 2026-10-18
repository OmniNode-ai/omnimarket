# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paths and apply decision for one Codex hook registration (OMN-18759).

Model ownership: PRIVATE to omnimarket.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCodexHookRegistrationRequest(BaseModel):
    """The plugin source, user hooks directory and injected backup timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_root: Path = Field(description="Absolute path to the onex plugin directory.")
    codex_home: Path = Field(description="Absolute path to the Codex user directory.")
    apply: bool = Field(default=False, description="Write missing hooks when true.")
    now: datetime = Field(
        description="Injected clock value, used only to name the backup."
    )

    @field_validator("plugin_root", "codex_home")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        """Require explicit absolute paths for both filesystem operands."""
        if not value.is_absolute():
            message = "path must be absolute"
            raise ValueError(message)
        return value
