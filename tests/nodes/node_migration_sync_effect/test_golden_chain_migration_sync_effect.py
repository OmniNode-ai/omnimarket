# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the effect's packaged contract resolves and its operations run (OMN-20687)."""

from __future__ import annotations

import hashlib
import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

NAME = "node_migration_sync_effect"


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


def test_contract_declares_command_and_terminal_topics_and_host_bound_runtime() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "EFFECT_GENERIC"
    runtime = contract["runtime_dispatch"]
    assert (
        runtime["command_topic"]
        == "onex.cmd.omnimarket.migration-sync-effect-requested.v1"
    )
    assert runtime["terminal_events"] == {
        "success": "onex.evt.omnimarket.migration-sync-effect-completed.v1",
        "failure": "onex.evt.omnimarket.migration-sync-effect-failed.v1",
    }
    # Host-bound: it reads one checkout and writes another, so no shared runtime attaches it.
    assert "event_bus" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [e["operation"] for e in routing["handlers"]] == [
        "read_migration_sync_inventory",
        "apply_migration_sync_plan",
    ]


def test_golden_chain_reads_an_inventory_then_applies_a_copy(tmp_path: Path) -> None:
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}
    src = tmp_path / "src" / "omnimarket" / "nodes" / "node_a" / "migrations"
    src.mkdir(parents=True)
    (src / "0001.sql").write_text("select 1;\n")
    dest = tmp_path / "dest"

    handler, request_type, result_type = _resolve(
        entries["read_migration_sync_inventory"]
    )
    inventory = handler().handle(
        request_type(
            omnimarket_src=str(tmp_path),
            allow_installed_package=False,
            dest_root=str(dest),
            manifest_path=str(tmp_path / "application-migrations.tsv"),
        )
    )
    assert isinstance(inventory, result_type)
    assert inventory.resolved is True
    assert [f.relative_path for f in inventory.source_files] == ["node_a/0001.sql"]
    assert (
        inventory.source_files[0].sha256 == hashlib.sha256(b"select 1;\n").hexdigest()
    )
    assert inventory.vendored_files == ()
    assert inventory.manifest_present is False

    from omnimarket.models.migration_sync import ModelMigrationSyncAction

    handler, request_type, result_type = _resolve(entries["apply_migration_sync_plan"])
    applied = handler().handle(
        request_type(
            dest_root=str(dest),
            actions=(
                ModelMigrationSyncAction(
                    kind="copy",
                    relative_path="node_a/0001.sql",
                    source_path=inventory.source_files[0].source_path,
                ),
            ),
        )
    )
    assert isinstance(applied, result_type)
    assert applied.ok is True
    assert (dest / "node_a" / "0001.sql").read_text() == "select 1;\n"
