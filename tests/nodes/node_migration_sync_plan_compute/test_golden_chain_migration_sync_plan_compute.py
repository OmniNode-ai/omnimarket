# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and plans a vendoring run (OMN-20687)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.migration_sync import (
    ModelMigrationSyncFile,
    ModelMigrationSyncInventory,
    ModelMigrationSyncManifestRow,
)

NAME = "node_migration_sync_plan_compute"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return contract


def _resolve(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    types = []
    for key in ("input_model", "output_model"):
        module, _, name = str(entry[key]).rpartition(".")
        types.append(getattr(importlib.import_module(module), name))
    return handler_type, types[0], types[1]


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.migration-sync-plan-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.migration-sync-planned.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [e["operation"] for e in routing["handlers"]] == ["plan_migration_sync"]


def test_golden_chain_plans_a_write_run_from_an_inventory() -> None:
    """A new source file is copied, a stale vendored file removed, a declared one kept."""
    entry = _contract()["handler_routing"]["handlers"][0]
    handler, request_type, result_type = _resolve(entry)
    inventory = ModelMigrationSyncInventory(
        resolved=True,
        nodes_dir="/src/omnimarket/nodes",
        dest_root="/infra/docker/migrations/forward/nodes",
        source_files=(
            ModelMigrationSyncFile(
                relative_path="node_a/0001.sql",
                sha256=SHA_A,
                source_path="/src/omnimarket/nodes/node_a/migrations/0001.sql",
            ),
        ),
        vendored_files=(
            ModelMigrationSyncFile(relative_path="node_old/0001.sql", sha256=SHA_B),
            ModelMigrationSyncFile(relative_path="node_kept/0001.sql", sha256=SHA_B),
        ),
        manifest_present=True,
        manifest_rows=(
            ModelMigrationSyncManifestRow(
                artifact_path="nodes/node_kept/0001.sql", declared_sha256=SHA_B
            ),
        ),
    )
    plan = handler().handle(request_type(mode="write", inventory=inventory))
    assert isinstance(plan, result_type)
    assert plan.exit_code == 0
    assert plan.verdict == "updated"
    assert [(a.kind, a.relative_path) for a in plan.actions] == [
        ("copy", "node_a/0001.sql"),
        ("remove", "node_old/0001.sql"),
    ]
    assert plan.changed_count == 2
    assert plan.lines[-1].text == "[sync-node-migrations] done: 2 file(s) updated."


def test_golden_chain_check_mode_changes_nothing_and_reports_drift() -> None:
    handler, request_type, _ = _resolve(_contract()["handler_routing"]["handlers"][0])
    inventory = ModelMigrationSyncInventory(
        resolved=True,
        nodes_dir="/n",
        dest_root="/d",
        source_files=(
            ModelMigrationSyncFile(
                relative_path="node_a/0001.sql",
                sha256=SHA_A,
                source_path="/n/node_a/migrations/0001.sql",
            ),
        ),
        vendored_files=(),
        manifest_present=False,
        manifest_rows=(),
    )
    plan = handler().handle(request_type(mode="check", inventory=inventory))
    assert (plan.exit_code, plan.verdict, plan.actions) == (1, "drift", ())
    assert plan.drift is True
