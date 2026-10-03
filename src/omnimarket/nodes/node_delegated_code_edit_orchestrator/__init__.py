# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegated code edit loop orchestrator node (OMN-20290).

A task and a git worktree in; a verified diff, a loop receipt and a tool_use
rubric verdict out. Each model turn is one ``onex delegate`` run whose reply is
a typed list of actions (view, ls, grep, write, edit, replace_in_files,
run_check, finish); the loop applies them confined to the worktree and the
declared writable paths, runs only declared checks, and ends accepted when
every check passes.
"""

from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.handler_delegated_code_edit_orchestrator import (
    HandlerDelegatedCodeEditOrchestrator,
    normalise_path,
    writable,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.turn_protocol import (
    RESPONSE_CONTRACT,
    TOOL_SCHEMAS,
    HistoryAction,
    HistoryTurn,
    build_turn_prompt,
    parse_turn_reply,
    render_history,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_ERROR_CHARS,
    EnumCodeEditStatus,
    EnumCodeEditTool,
    ModelCheckResult,
    ModelCodeEditAction,
    ModelCodeEditResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelObservation,
    ModelTurnReply,
    bound_error,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.protocols.protocol_delegated_code_edit_ports import (
    LoopReceiptExistsError,
    ProtocolDelegatedCodeEditPorts,
    ResumeRefusedError,
    WorkspacePathError,
)


class NodeDelegatedCodeEditOrchestrator(HandlerDelegatedCodeEditOrchestrator):
    """ONEX entry-point wrapper for HandlerDelegatedCodeEditOrchestrator."""


__all__ = [
    "MAX_ERROR_CHARS",
    "RESPONSE_CONTRACT",
    "TOOL_SCHEMAS",
    "EnumCodeEditStatus",
    "EnumCodeEditTool",
    "HandlerDelegatedCodeEditOrchestrator",
    "HistoryAction",
    "HistoryTurn",
    "LoopReceiptExistsError",
    "ModelCheckResult",
    "ModelCodeEditAction",
    "ModelCodeEditResult",
    "ModelDeclaredCheck",
    "ModelDelegatedCodeEditRequest",
    "ModelObservation",
    "ModelTurnReply",
    "NodeDelegatedCodeEditOrchestrator",
    "ProtocolDelegatedCodeEditPorts",
    "ResumeRefusedError",
    "WorkspacePathError",
    "bound_error",
    "build_turn_prompt",
    "normalise_path",
    "parse_turn_reply",
    "render_history",
    "writable",
]
