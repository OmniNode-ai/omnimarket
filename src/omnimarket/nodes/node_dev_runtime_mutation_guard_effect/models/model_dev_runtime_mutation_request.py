# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One PreToolUse Bash call, as the dev runtime mutation guard judges it (OMN-21795).

Model ownership: PRIVATE to omnimarket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.enums import (
    EnumGuardSessionMode,
)


class ModelDevRuntimeMutationRequest(BaseModel):
    """The tool call and the session facts the guard needs. No environment value is carried."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(description="The PreToolUse tool_name.")
    command: str = Field(default="", description="tool_input.command of a Bash call.")
    cwd: str = Field(default="", description="The caller's working directory.")
    mode: EnumGuardSessionMode = Field(
        default=EnumGuardSessionMode.FULL,
        description="omniclaude_mode of the session: lite (a non-OmniNode install) is never refused.",
    )
