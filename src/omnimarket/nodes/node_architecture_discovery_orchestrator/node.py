"""Declarative shell; the contract and handler own discovery behavior."""

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator


class NodeArchitectureDiscoveryOrchestrator(NodeOrchestrator):
    """Four scans, adjudication and report, declared in contract.yaml."""
