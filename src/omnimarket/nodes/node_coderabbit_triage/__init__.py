"""node_coderabbit_triage — CodeRabbit thread classification WorkflowPackage."""

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    EnumThreadSeverity,
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
    ModelCoderabbitTriageResult,
    ModelThreadClassification,
)

__all__ = [
    "NodeCoderabbitTriage",
    "EnumThreadSeverity",
    "HandlerCoderabbitTriage",
    "ModelCoderabbitTriageCommand",
    "ModelCoderabbitTriageResult",
    "ModelThreadClassification",
]

class NodeCoderabbitTriage(HandlerCoderabbitTriage):
    """ONEX entry-point wrapper for HandlerCoderabbitTriage."""
