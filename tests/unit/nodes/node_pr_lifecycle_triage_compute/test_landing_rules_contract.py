# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing red rules are ten declared, pure, definition-B operations with no topic of their own (OMN-20745).

The landing controller calls them in process through its decision bridge, so the contract-topic graph gains no
edge: the node's bus surface is what it was (one subscribe topic, one publish topic), and none of the new
operations is routed.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

import omnimarket.nodes.node_pr_lifecycle_triage_compute as node_pkg

pytestmark = pytest.mark.unit

_NODE_DIR = Path(node_pkg.__file__).parent
_RULE_OPERATIONS = (
    "classify_landing_red",
    "classify_cascade_checks",
    "list_cancelled_checks",
    "apply_reviewer_pool",
    "count_reviewer_runs_in_flight",
    "fired_edge_graded_stale",
    "classify_companion_wait",
    "classify_stale_summary",
    "next_stale_step",
    "list_pending_required",
)
# a pure handler imports no transport, process, clock or filesystem module
_IMPURE = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiokafka",
    "asyncio",
    "pathlib",
    "time",
}


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def _operation(name: str) -> dict[str, Any]:
    entries = [op for op in _contract()["operations"] if op["name"] == name]
    assert len(entries) == 1, f"{name} must be declared exactly once"
    entry = entries[0]
    assert isinstance(entry, dict)
    return entry


def _resolve(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("name", _RULE_OPERATIONS)
def test_the_operation_is_declared_once_with_models_and_a_handler_that_resolve(
    name: str,
) -> None:
    op = _operation(name)
    for key in ("input_model", "output_model"):
        model = _resolve(op[key]["module"], op[key]["name"])
        assert issubclass(model, BaseModel)
    handler = _resolve(op["handler"]["module"], op["handler"]["name"])
    assert inspect.isclass(handler)


@pytest.mark.parametrize("name", _RULE_OPERATIONS)
def test_the_handler_is_definition_b_handle_request_to_response(name: str) -> None:
    op = _operation(name)
    handler = _resolve(op["handler"]["module"], op["handler"]["name"])
    signature = inspect.signature(handler.handle)
    assert list(signature.parameters) == ["self", "request"]
    hints = {
        "request": _resolve(op["input_model"]["module"], op["input_model"]["name"]),
        "return": _resolve(op["output_model"]["module"], op["output_model"]["name"]),
    }
    annotations = inspect.get_annotations(handler.handle, eval_str=True)
    assert annotations == hints


@pytest.mark.parametrize("name", _RULE_OPERATIONS)
def test_the_handler_module_is_pure(name: str) -> None:
    module = _operation(name)["handler"]["module"]
    tree = ast.parse(Path(inspect.getfile(importlib.import_module(module))).read_text())
    imported = {
        n.module.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
    }
    imported |= {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    }
    assert not imported & _IMPURE, f"{module} imports {sorted(imported & _IMPURE)}"


def test_no_rule_operation_is_routed_and_the_node_gains_no_topic() -> None:
    contract = _contract()
    routed = {h["operation"] for h in contract["handler_routing"]["handlers"]}
    assert not routed & set(_RULE_OPERATIONS)
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1"
    ]


def test_the_red_class_enum_is_the_controllers_vocabulary() -> None:
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_landing_red_class import (
        EnumLandingRedClass,
    )

    assert {m.value for m in EnumLandingRedClass} == {
        "product",
        "cascade",
        "runner_saturation",
        "cancelled_producer",
        "reviewer_pool",
    }


_LIGHT_PREFIX = "omnimarket.nodes.node_pr_lifecycle_triage_compute."


def _local_closure(module: str, seen: set[str]) -> set[str]:
    """The modules of this node a rule handler reaches by import, and every third-party root on the way."""
    if module in seen:
        return set()
    seen.add(module)
    tree = ast.parse(Path(inspect.getfile(importlib.import_module(module))).read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        names = (
            [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        )
        names += [a.name for a in node.names] if isinstance(node, ast.Import) else []
        for name in names:
            if name.startswith(_LIGHT_PREFIX):
                roots |= _local_closure(name, seen)
            else:
                roots.add(name.split(".")[0])
    return roots


@pytest.mark.parametrize("name", _RULE_OPERATIONS)
def test_the_rule_handlers_import_only_pydantic_and_this_nodes_own_modules(
    name: str,
) -> None:
    """The controller runs them under an interpreter that holds pydantic and this node's files, nothing else.

    ``omnibase_core``, ``omnimarket.events`` and ``omnimarket.merge_control`` are not there, so a rule handler that
    reached one would break the controller's tick, not a test. (The handlers package resolves
    ``HandlerClassifyHeadChecks`` on first use for the same reason.)
    """
    handler_module = _operation(name)["handler"]["module"]
    roots = _local_closure(handler_module, set())
    third_party = {
        r
        for r in roots
        if r not in {"__future__", "collections", "datetime", "enum", "re", "typing"}
    }
    assert third_party <= {"pydantic"}, (
        f"{handler_module} reaches {sorted(third_party - {'pydantic'})}"
    )


def test_the_rules_load_with_the_heavy_packages_blocked() -> None:
    """The controller's loader binds this node's package without running its ``__init__`` (which pulls the batch
    triage handler and with it ``omnimarket.events``), then imports the rule handlers. With the packages the
    decision runtime lacks blocked, all ten still import and answer."""
    code = """
import importlib, sys, types
for blocked in ("omnibase_core", "omnimarket.events", "omnimarket.merge_control", "omnimarket.models"):
    sys.modules[blocked] = None
import omnimarket.nodes as nodes
node = "omnimarket.nodes.node_pr_lifecycle_triage_compute"
pkg = types.ModuleType(node)
pkg.__path__ = [sys.argv[1]]
sys.modules[node] = pkg
for module in sys.argv[2:]:
    importlib.import_module(module)
print("ok", len(sys.argv) - 2)
"""
    modules = [_operation(name)["handler"]["module"] for name in _RULE_OPERATIONS]
    proc = subprocess.run(
        [sys.executable, "-c", code, str(_NODE_DIR), *modules],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"ok {len(_RULE_OPERATIONS)}"
