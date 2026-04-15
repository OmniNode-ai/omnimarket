"""Tests asserting node_golden_chain_sweep is registered in the onex workflow registry.

DoD-002: pytest asserts node_golden_chain_sweep is present in the workflow registry.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

WORKFLOW_YAML = Path(__file__).parent.parent / "golden_chain_sweep_workflow.yaml"
REGISTERED_CHAINS = [
    "overnight",
    "pr_lifecycle",
    "delegation",
    "aislop_sweep",
    "autopilot",
    "overseer_verifier",
]


@pytest.mark.unit
class TestWorkflowRegistry:
    """Assert node_golden_chain_sweep is discoverable via onex workflow registry."""

    def test_node_in_entry_points(self) -> None:
        """node_golden_chain_sweep must be registered under onex.nodes entry-point group."""
        eps = entry_points(group="onex.nodes")
        names = {ep.name for ep in eps}
        assert "node_golden_chain_sweep" in names, (
            f"node_golden_chain_sweep not found in onex.nodes entry points. "
            f"Registered: {sorted(names)}"
        )

    def test_entry_point_resolves_to_package(self) -> None:
        """Entry point must resolve to the package directory (not a class)."""
        eps = entry_points(group="onex.nodes")
        ep = next((e for e in eps if e.name == "node_golden_chain_sweep"), None)
        assert ep is not None
        assert ep.value == "omnimarket.nodes.node_golden_chain_sweep", (
            f"Expected package-form entry point, got: {ep.value}"
        )

    def test_workflow_yaml_exists(self) -> None:
        """golden_chain_sweep_workflow.yaml must exist at repo root."""
        assert WORKFLOW_YAML.exists(), (
            f"Workflow contract not found: {WORKFLOW_YAML}. "
            "Create golden_chain_sweep_workflow.yaml to enable onex run dispatch."
        )

    def test_workflow_yaml_is_valid(self) -> None:
        """golden_chain_sweep_workflow.yaml must be parseable YAML with required fields."""
        assert WORKFLOW_YAML.exists()
        data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data.get("name") == "golden_chain_sweep_workflow"
        assert "handler" in data
        assert "terminal_event" in data

    def test_workflow_yaml_handler_module(self) -> None:
        """Workflow YAML handler must point to the correct module and class."""
        data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        handler = data.get("handler", {})
        assert handler.get("module") == (
            "omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep"
        )
        assert handler.get("class") == "NodeGoldenChainSweep"

    def test_workflow_yaml_registers_all_chains(self) -> None:
        """Workflow YAML defaults must declare all required chains."""
        data = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        chains = defaults.get("chains", [])
        chain_names = {c["name"] for c in chains}
        missing = set(REGISTERED_CHAINS) - chain_names
        unexpected = chain_names - set(REGISTERED_CHAINS)
        assert chain_names == set(REGISTERED_CHAINS), (
            f"Chain mismatch — missing: {sorted(missing)}, unexpected: {sorted(unexpected)}"
        )

    def test_handler_importable(self) -> None:
        """NodeGoldenChainSweep handler must be importable."""
        from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
            NodeGoldenChainSweep,
        )

        assert callable(getattr(NodeGoldenChainSweep, "handle", None))

    def test_contract_yaml_exists(self) -> None:
        """node_golden_chain_sweep must have a contract.yaml alongside the package."""
        ep = next(
            (
                e
                for e in entry_points(group="onex.nodes")
                if e.name == "node_golden_chain_sweep"
            ),
            None,
        )
        assert ep is not None
        mod = ep.load()
        import inspect

        pkg_dir = Path(inspect.getfile(mod)).parent
        contract = pkg_dir / "contract.yaml"
        assert contract.exists(), f"contract.yaml not found at {contract}"
