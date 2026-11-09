# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, policy and decision of the agent-prompt gh-read guard (OMN-23307)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.enums import (
    EnumAgentPromptGhReadVerdict,
    EnumGuardSessionMode,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.models.model_gh_read_scan import (
    ModelGhReadFinding,
)


class ModelAgentPromptGhReadPolicy(BaseModel):
    """Which tool inputs are judged, declared in the contract and extended by the overlay.

    ``judged_fields`` maps a tool name to the ``tool_input`` fields whose text a subagent
    will follow; ``code_fields`` names the fields that hold code (a workflow script), where
    comment lines are not instructions; ``path_fields`` names the fields that hold the path
    of such a script, read by the runtime entry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    judged_fields: dict[str, tuple[str, ...]]
    code_fields: tuple[str, ...] = ()
    path_fields: dict[str, str] = Field(default_factory=dict)
    refusal_class: str = Field(min_length=1)


class ModelAgentPromptSurface(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    text: str
    is_code: bool = False


class ModelAgentPromptGhReadRequest(BaseModel):
    """One Agent or Workflow dispatch: the texts a subagent will be told to follow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    tool_name: str = Field(min_length=1)
    surfaces: tuple[ModelAgentPromptSurface, ...] = ()
    mode: EnumGuardSessionMode = EnumGuardSessionMode.FULL


class ModelAgentPromptGhReadDecision(BaseModel):
    """ALLOW, or REFUSE with every offending line and the mirror that answers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumAgentPromptGhReadVerdict
    findings: tuple[ModelGhReadFinding, ...] = ()
    reason: str = ""
    refusal_class: str = ""

    def hook_payload(self) -> dict[str, object]:
        """The JSON the hook prints on a refusal: its keys are the wire contract."""
        return {
            "decision": "block",
            "reason": self.reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": self.reason,
            },
        }
