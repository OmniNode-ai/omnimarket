# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One agentic run as the caller recorded it, for the tool_use rubric class.

The caller supplies every fact here; the compute reads nothing else. Paths in
``workspace_files`` and in tool-call arguments must share one root (the caller
normalises them), since the compute compares them as strings. ``workspace_files``
None means no manifest was supplied, which is a different fact from an empty
tree, and leaves path checks undetermined. ``engine`` is the model id the run
recorded (crush's message ``model``, Claude Code's ``init`` model); the budget
criterion reads its wall-time limit by it.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_declared_tool import (
    ModelDeclaredTool,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_tool_call import (
    ModelToolCall,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_workspace_file import (
    ModelWorkspaceFile,
)


class ModelToolUseTranscript(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_tools: tuple[ModelDeclaredTool, ...]
    tool_calls: tuple[ModelToolCall, ...]
    turn_count: int = Field(ge=0)
    wall_time_ms: int | None = Field(default=None, ge=0)
    workspace_files: tuple[ModelWorkspaceFile, ...] | None = None
    engine: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        for label, names in (
            ("tool names", [row.name for row in self.declared_tools]),
            ("call ids", [row.call_id for row in self.tool_calls]),
            ("workspace paths", [row.path for row in self.workspace_files or ()]),
        ):
            if len(names) != len(set(names)):
                raise ValueError("duplicate " + label)
        return self
