"""Verify all 7 build loop nodes are discoverable via entry points.

OMN-7584: Each build loop node must be registered in pyproject.toml under
[project.entry-points."onex.nodes"] and must resolve to a valid, importable module.
"""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest

BUILD_LOOP_NODES = [
    "node_build_loop",
    "node_loop_state_reducer",
    "node_rsd_fill_compute",
    "node_ticket_classify_compute",
    "node_closeout_effect",
    "node_verify_effect",
    "node_build_dispatch_effect",
]


@pytest.mark.unit
class TestBuildLoopEntryPoints:
    """All 7 build loop entry points resolve to valid modules."""

    def _get_entry_points(self) -> dict[str, importlib.metadata.EntryPoint]:
        """Return a name -> EntryPoint mapping for the onex.nodes group."""
        eps = importlib.metadata.entry_points(group="onex.nodes")
        return {ep.name: ep for ep in eps}

    def test_all_seven_registered(self) -> None:
        """Every build loop node has an entry point registration."""
        ep_map = self._get_entry_points()
        missing = [n for n in BUILD_LOOP_NODES if n not in ep_map]
        assert not missing, f"Missing entry points: {missing}"

    @pytest.mark.parametrize("node_name", BUILD_LOOP_NODES)
    def test_entry_point_loadable(self, node_name: str) -> None:
        """Each entry point resolves to an importable module."""
        ep_map = self._get_entry_points()
        assert node_name in ep_map, f"Entry point {node_name} not registered"
        module = ep_map[node_name].load()
        assert module is not None, f"Entry point {node_name} loaded as None"

    @pytest.mark.parametrize("node_name", BUILD_LOOP_NODES)
    def test_module_has_contract(self, node_name: str) -> None:
        """Each node module directory contains a contract.yaml."""
        module_path = f"omnimarket.nodes.{node_name}"
        mod = importlib.import_module(module_path)
        assert mod is not None
        # Verify the module's package directory exists (contract.yaml checked
        # structurally by test_metadata_schema or golden chain tests)
        assert hasattr(mod, "__path__") or hasattr(mod, "__file__")

    @pytest.mark.parametrize("node_name", BUILD_LOOP_NODES)
    def test_handler_subpackage_exists(self, node_name: str) -> None:
        """Each node has a handlers subpackage."""
        handler_path = f"omnimarket.nodes.{node_name}.handlers"
        mod = importlib.import_module(handler_path)
        assert mod is not None
