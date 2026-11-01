# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev runtime mutation guard's verdict for one Bash command (OMN-21795).

Model ownership: PRIVATE to omnimarket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.enums import (
    EnumDevRuntimeMutationVerdict,
)


class ModelDevRuntimeMutationDecision(BaseModel):
    """ALLOW or REFUSE, the protected project and the mutation judged, and the refusal text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumDevRuntimeMutationVerdict
    project: str = Field(
        default="", description="The protected project the command targets."
    )
    mutation: str = Field(
        default="", description="The docker invocation judged, as one short phrase."
    )
    granted: bool = Field(
        default=False,
        description="A live grant row named the project, so the call passed.",
    )
    reason: str = Field(default="", description="The refusal text; empty on ALLOW.")

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
