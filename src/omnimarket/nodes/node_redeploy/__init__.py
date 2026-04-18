"""node_redeploy — WorkflowPackage for full post-release runtime redeploy."""

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy import HandlerRedeploy
from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    HandlerRedeployKafka,
)
from omnimarket.nodes.node_redeploy.handlers.handler_workflow_runner import (
    HandlerRedeployWorkflowRunner,
    ModelRedeployWorkflowInput,
    ModelRedeployWorkflowResult,
    run_redeploy_workflow,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelRedeployResult,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    ModelRedeployCompletedEvent,
    ModelRedeployStartCommand,
)

__all__ = [
    "HandlerRedeploy",
    "HandlerRedeployKafka",
    "HandlerRedeployWorkflowRunner",
    "ModelDeployRebuildCommand",
    "ModelDeployRebuildCompleted",
    "ModelRedeployCompletedEvent",
    "ModelRedeployResult",
    "ModelRedeployStartCommand",
    "ModelRedeployWorkflowInput",
    "ModelRedeployWorkflowResult",
    "NodeRedeploy",
    "run_redeploy_workflow",
]


class NodeRedeploy(HandlerRedeployWorkflowRunner):
    """ONEX entry-point wrapper for HandlerRedeployWorkflowRunner."""
