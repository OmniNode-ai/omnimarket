# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What merging one Codex hook registration did (OMN-18759).

Model ownership: PRIVATE to omnimarket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_codex_hook_registration_effect.enums import (
    EnumCodexHookRegistrationStatus,
)


class ModelCodexHookRegistrationResult(BaseModel):
    """The outcome, target file, added commands and backup, when one was needed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumCodexHookRegistrationStatus
    exit_code: int = Field(description="0 unless the registration could not be merged.")
    hooks_file: str = Field(description="The Codex user hooks file.")
    added_commands: tuple[str, ...] = Field(
        default=(), description="Commands added in registration order."
    )
    backup_path: str = Field(
        default="", description="The original file's backup, if any."
    )
    messages: tuple[str, ...] = Field(
        default=(), description="Diagnostics for the caller."
    )
