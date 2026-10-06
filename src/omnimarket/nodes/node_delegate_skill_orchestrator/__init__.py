# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegate_skill_orchestrator — consumer-facing delegation entry point.

Public API surface for the delegation thin-shim node. Claude Code and Codex
adapters dispatch typed delegation requests here; the handler translates them to
runtime-internal commands through an injected dispatch port.
"""

from __future__ import annotations

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)

__all__ = [
    "HandlerDelegateSkill",
    "ModelDelegateSkillRequest",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
    "ProtocolDelegationDispatchPort",
]
