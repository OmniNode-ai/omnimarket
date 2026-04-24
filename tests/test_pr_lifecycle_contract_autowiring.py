from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

NODES_ROOT = Path("src/omnimarket/nodes")
PR_LIFECYCLE_CONTRACTS = (
    "node_pr_lifecycle_fix_effect",
    "node_pr_lifecycle_inventory_compute",
    "node_pr_lifecycle_merge_effect",
    "node_pr_lifecycle_orchestrator",
    "node_pr_lifecycle_state_reducer",
    "node_pr_lifecycle_triage_compute",
)


def _load_contract(node_name: str) -> dict[str, Any]:
    contract_path = NODES_ROOT / node_name / "contract.yaml"
    with contract_path.open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict), f"{contract_path} must load as a YAML mapping"
    return contract


def _assert_importable_dotted_class(dotted_class: str) -> None:
    module_name, _, class_name = dotted_class.rpartition(".")
    assert module_name, f"{dotted_class} must include a module path"
    module = importlib.import_module(module_name)
    assert hasattr(module, class_name), f"{dotted_class} is not importable"


def test_pr_lifecycle_contracts_are_runtime_autowireable() -> None:
    for node_name in PR_LIFECYCLE_CONTRACTS:
        contract = _load_contract(node_name)
        event_bus = contract.get("event_bus")
        assert isinstance(event_bus, dict), f"{node_name} must declare event_bus"
        assert event_bus.get("subscribe_topics"), (
            f"{node_name} must declare event_bus.subscribe_topics for runtime wiring"
        )
        assert "subscribe_topics" not in contract, (
            f"{node_name} must not use legacy top-level subscribe_topics"
        )
        assert "publish_topics" not in contract, (
            f"{node_name} must not use legacy top-level publish_topics"
        )

        routing = contract.get("handler_routing")
        assert isinstance(routing, dict), f"{node_name} must declare handler_routing"
        handlers = routing.get("handlers")
        assert isinstance(handlers, list), (
            f"{node_name} must declare handler_routing.handlers as a list"
        )
        assert handlers, f"{node_name} must declare handler_routing.handlers"
        for entry in handlers:
            handler_ref = entry.get("handler")
            assert isinstance(handler_ref, dict), (
                f"{node_name} handler entries must declare handler refs"
            )
            _assert_importable_dotted_class(
                f"{handler_ref['module']}.{handler_ref['name']}"
            )


def test_pr_lifecycle_orchestrator_default_impls_are_importable() -> None:
    contract = _load_contract("node_pr_lifecycle_orchestrator")
    dependencies = contract.get("sub_handler_dependencies")
    assert isinstance(dependencies, list)
    assert dependencies
    for dependency in dependencies:
        default_impl = dependency.get("default_impl")
        assert isinstance(default_impl, str)
        _assert_importable_dotted_class(default_impl)
