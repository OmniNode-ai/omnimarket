"""Discovers onex.nodes entry points and their golden chain test modules."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib.metadata import entry_points


@dataclass
class NodeTestMapping:
    """Maps a node entry point to its golden chain test module."""

    node_name: str
    handler_class_path: (
        str  # e.g. "omnimarket.nodes.node_foo.handlers.handler_foo:HandlerFoo"
    )
    test_module_path: (
        str | None
    )  # e.g. "tests.test_golden_chain_foo", or None if absent


def _golden_chain_module_for(node_name: str, package_root: str) -> str | None:
    """Return dotted test module path if a golden chain exists for node_name, else None.

    Strategy: try to import tests.test_golden_chain_<suffix> where suffix = node_name
    stripped of the 'node_' prefix. Falls back to scanning as installed package module.
    """
    suffix = node_name.removeprefix("node_")
    candidate = f"tests.test_golden_chain_{suffix}"
    try:
        importlib.import_module(candidate)
        return candidate
    except ModuleNotFoundError:
        pass
    # Try as installed package test: omnimarket.tests.test_golden_chain_<suffix>
    candidate2 = f"{package_root}.tests.test_golden_chain_{suffix}"
    try:
        importlib.import_module(candidate2)
        return candidate2
    except ModuleNotFoundError:
        pass
    return None


def discover_node_test_modules(
    package_root: str = "omnimarket",
    feature: str | None = None,
) -> list[NodeTestMapping]:
    """Discover all onex.nodes entry points and map each to its golden chain test module.

    Args:
        package_root: The top-level package name to search (default: "omnimarket").
        feature: If set, return only the mapping for this single node name.

    Returns:
        List of NodeTestMapping, one per registered entry point.
    """
    eps = entry_points(group="onex.nodes")
    mappings: list[NodeTestMapping] = []

    for ep in eps:
        node_name = ep.name
        if feature and node_name != feature:
            continue
        handler_path = ep.value
        # Only include nodes from the target package
        if not handler_path.startswith(package_root + "."):
            continue
        test_mod = _golden_chain_module_for(node_name, package_root)
        mappings.append(
            NodeTestMapping(
                node_name=node_name,
                handler_class_path=handler_path,
                test_module_path=test_mod,
            )
        )

    return mappings
