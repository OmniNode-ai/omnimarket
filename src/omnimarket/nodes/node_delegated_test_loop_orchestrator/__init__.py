# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegated test loop orchestrator node.

A local model writes a test for one acceptance criterion, a throwaway container
on a lab host runs it, failures go back to the model up to three calls, and a
must-fail control runs the passing test against the pre-fix commit (or a
mutation of the fixed commit). The caller reads one compact result.
"""

from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.handler_delegated_test_loop_orchestrator import (
    HandlerDelegatedTestLoopOrchestrator,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.replay import (
    replay_loop_receipt,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.reply_parser import (
    parse_test_reply,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    EnumLoopStatus,
    ModelControlVerdict,
    ModelDelegatedTestLoopRequest,
    ModelDelegatedTestLoopResult,
    ModelDelegateReply,
    ModelLoopMutation,
    ModelRunDigest,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    LoopReceiptExistsError,
    ModelPrompt,
    ModelRunReceipt,
    ProtocolDelegatedTestLoopPorts,
)


class NodeDelegatedTestLoopOrchestrator(HandlerDelegatedTestLoopOrchestrator):
    """ONEX entry-point wrapper for HandlerDelegatedTestLoopOrchestrator."""


__all__ = [
    "EnumLoopStatus",
    "HandlerDelegatedTestLoopOrchestrator",
    "LoopReceiptExistsError",
    "ModelControlVerdict",
    "ModelDelegateReply",
    "ModelDelegatedTestLoopRequest",
    "ModelDelegatedTestLoopResult",
    "ModelLoopMutation",
    "ModelPrompt",
    "ModelRunDigest",
    "ModelRunReceipt",
    "NodeDelegatedTestLoopOrchestrator",
    "ProtocolDelegatedTestLoopPorts",
    "parse_test_reply",
    "replay_loop_receipt",
]
