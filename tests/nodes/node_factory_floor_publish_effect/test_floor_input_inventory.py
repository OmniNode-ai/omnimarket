# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's input inventory (OMN-23391, AC1) holds every step and every path the floor nodes' contracts declare.

A step or a path added to a contract with no row in the inventory fails here, and so does a file read that has no replacement
node, no snapshot topic, no order or no ticket.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

FLOOR_NODES = (
    "node_factory_floor_publish_effect",
    "node_factory_floor_effect",
    "node_floor_simple_effect",
    "node_floor_compare_effect",
    "node_floor_delegation_effect",
    "node_work_state_projection_effect",
    "node_floor_projection_read_effect",
    "node_floor_owner_map_publish_effect",
)
FILE_CLASSES = {"file", "jsonl", "directory", "command", "ssh", "http"}
TICKET = re.compile(r"^(OMN-\d+|NEED-CHILD-TICKET:[a-z0-9-]+|none)$")
TOPIC = re.compile(r"^(onex\.snapshot\.projection\.[a-z0-9.-]+\.v1|none)$")


def _load(package: str, name: str) -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{package}")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def _inventory() -> list[dict[str, Any]]:
    doc = _load("node_factory_floor_publish_effect", "inventory/floor_inputs.yaml")
    return list(doc["inputs"])


def _paths(node: Any, key: str = "") -> list[str]:
    """Every string a contract's ``runtime`` block gives under a key that names a path, a glob or a file."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found.extend(_paths(v, str(k)))
    elif isinstance(node, list):
        for v in node:
            found.extend(_paths(v, key))
    elif isinstance(node, str) and (
        key.endswith(("relpath", "glob", "_name", "_file", "_path"))
        and key not in {"chain", "group_prefix"}
    ):
        found.append(node)
    return found


def test_every_step_the_publish_runs_has_a_row() -> None:
    runtime = _load("node_factory_floor_publish_effect", "contract.yaml")["runtime"]
    steps = [s["name"] for s in runtime["steps"] + runtime["after_publish_steps"]]
    named = {step for row in _inventory() for step in row["steps"]}
    assert [s for s in steps if s not in named] == []


def test_every_path_the_floor_contracts_declare_is_in_a_row() -> None:
    wheres = " ".join(str(row["where"]) for row in _inventory())
    missing: list[str] = []
    for node in FLOOR_NODES:
        runtime = _load(node, "contract.yaml").get("runtime", {})
        for path in _paths(runtime):
            if path.startswith("page/") or path in {
                "ledger",
                "archive",
                "node_factory_floor_compute/contract.yaml",
            }:
                continue
            probe = path.split("*")[0].rstrip("/") or path
            if probe not in wheres:
                missing.append(f"{node}: {path}")
    assert missing == []


def test_every_file_read_has_a_replacement_a_topic_an_order_and_a_ticket() -> None:
    bad: list[str] = []
    for row in _inventory():
        replacement = row["replacement"]
        if not (
            replacement.get("node")
            and TOPIC.match(str(replacement.get("topic")))
            and replacement.get("status") in {"exists", "new"}
            and isinstance(row["order"], int)
            and TICKET.match(str(row["ticket"]))
        ):
            bad.append(row["id"])
        if row["class"] in FILE_CLASSES and (
            row["ticket"] == "none" or replacement["topic"] == "none"
        ):
            bad.append(f"{row['id']} (a read with nothing to replace it)")
    assert bad == []


def test_the_rows_are_in_the_order_they_move_and_the_ids_are_unique() -> None:
    rows = [r for r in _inventory() if r["class"] != "config"]
    orders = [r["order"] for r in rows if r["id"] != "floor_projection_bundle"]
    assert orders == sorted(orders)
    ids = [r["id"] for r in _inventory()]
    assert len(ids) == len(set(ids))


def test_named_break_a_step_with_no_row_fails_the_inventory() -> None:
    runtime = _load("node_factory_floor_publish_effect", "contract.yaml")["runtime"]
    steps = [s["name"] for s in runtime["steps"]] + ["a_new_step_nobody_listed"]
    named = {step for row in _inventory() for step in row["steps"]}
    assert [s for s in steps if s not in named] == ["a_new_step_nobody_listed"]
