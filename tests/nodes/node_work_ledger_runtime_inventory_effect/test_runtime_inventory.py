# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schema and reconciliation checks of the runtime inventory reader, over a small neutral fixture."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.handlers.handler_runtime_inventory import (
    INVENTORY_FILE_ENV,
    OVERLAY_ROOTS_ENV,
    HandlerRuntimeInventory,
)
from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.models.model_runtime_inventory import (
    ModelRuntimeInventoryRequest,
    ModelRuntimeInventoryResult,
)
from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.runtime_inventory_check import (
    main,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "runtime_inventory"
OVERLAY_DIR = "work_ledger_runtime_inventory"

Edit = Callable[[dict[str, Any], dict[str, Any]], None]


def _run(root: Path) -> ModelRuntimeInventoryResult:
    return HandlerRuntimeInventory().handle(
        ModelRuntimeInventoryRequest(overlay_roots=[str(root)])
    )


def _edited(tmp_path: Path, edit: Edit) -> ModelRuntimeInventoryResult:
    """Copy the fixture, apply ``edit`` to the parsed inventory and snapshot, run the check."""
    root = tmp_path / "root"
    shutil.copytree(FIXTURE_ROOT, root)
    inventory_file = root / OVERLAY_DIR / "inventory.yaml"
    snapshot_file = root / OVERLAY_DIR / "host_snapshot.yaml"
    inventory = yaml.safe_load(inventory_file.read_text())
    snapshot = yaml.safe_load(snapshot_file.read_text())
    edit(inventory, snapshot)
    inventory_file.write_text(yaml.safe_dump(inventory))
    snapshot_file.write_text(yaml.safe_dump(snapshot))
    return _run(root)


def _has(result: ModelRuntimeInventoryResult, fragment: str) -> bool:
    return any(fragment in finding for finding in result.findings)


def test_runtime_inventory_fixture_passes_with_counts() -> None:
    result = _run(FIXTURE_ROOT)
    assert result.findings == []
    assert (result.verdict, result.row_count, result.exclusion_count) == ("PASS", 3, 4)


@pytest.mark.parametrize(
    "field", ["id", "host", "lane", "overlay", "current_tenant", "forwarder_instance"]
)
def test_runtime_inventory_blank_row_field_fails(tmp_path: Path, field: str) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1][field] = "  "

    result = _edited(tmp_path, edit)
    assert result.verdict == "FAIL"
    assert _has(result, f"{field} is missing or blank")


def test_runtime_inventory_distinct_tenant_duplicate_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["proposed_tenant_slug"] = inv["rows"][0]["proposed_tenant_slug"]

    assert _has(_edited(tmp_path, edit), "duplicate tenant slug")


def test_runtime_inventory_distinct_tenant_blank_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["proposed_tenant_slug"] = ""

    assert _has(_edited(tmp_path, edit), "proposed_tenant_slug is missing or blank")


def test_runtime_inventory_distinct_tenant_reserved_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["proposed_tenant_slug"] = "tenant-house"

    assert _has(_edited(tmp_path, edit), "reuses a reserved tenant slug")


@pytest.mark.parametrize("slug", ["Slug_B", "-slug", "slug-", "a" * 64, "slug b"])
def test_runtime_inventory_distinct_tenant_not_dns_label_fails(
    tmp_path: Path, slug: str
) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["proposed_tenant_slug"] = slug

    assert _has(_edited(tmp_path, edit), "is not a DNS-style label")


def test_runtime_inventory_duplicate_row_id_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["id"] = inv["rows"][0]["id"]

    assert _has(_edited(tmp_path, edit), "duplicate row id")


def test_runtime_inventory_unmatched_observed_token_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        snap["observed"].append("compose_project:project-new@host-1")

    result = _edited(tmp_path, edit)
    assert _has(result, "unmatched: compose_project:project-new@host-1")


def test_runtime_inventory_dropped_exclusion_leaves_unmatched(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["exclusions"] = [e for e in inv["exclusions"] if e["id"] != "excl-host-3"]

    result = _edited(tmp_path, edit)
    assert _has(result, "unmatched: writer_host:host-3")
    assert _has(result, "no exclusion of required class 'in-the-shop'")


def test_runtime_inventory_stale_row_match_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        snap["observed"].remove("compose_project:project-b@host-1")

    assert _has(
        _edited(tmp_path, edit),
        "stale: row runtime-b matches compose_project:project-b@host-1",
    )


def test_runtime_inventory_row_and_exclusion_conflict_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["exclusions"][2]["matches"].append("compose_project:project-a@host-1")

    assert _has(_edited(tmp_path, edit), "claimed by both a row and an exclusion")


def test_runtime_inventory_two_rows_one_token_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["rows"][1]["matches"].append("manifest_lane:lane-a")

    assert _has(_edited(tmp_path, edit), "claimed by rows runtime-a, runtime-b")


def test_runtime_inventory_exclusion_without_reason_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["exclusions"][2]["reason"] = ""

    assert _has(_edited(tmp_path, edit), "reason is missing or blank")


def test_runtime_inventory_required_class_missing_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        inv["exclusions"] = [e for e in inv["exclusions"] if e["class"] != "production"]

    assert _has(_edited(tmp_path, edit), "no exclusion of required class 'production'")


def test_runtime_inventory_wrong_schema_version_fails(tmp_path: Path) -> None:
    def edit(inv: dict[str, Any], snap: dict[str, Any]) -> None:
        snap["schema_version"] = "other"

    assert _has(_edited(tmp_path, edit), "schema_version must be")


def test_runtime_inventory_no_declared_location_fails_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(INVENTORY_FILE_ENV, raising=False)
    monkeypatch.delenv(OVERLAY_ROOTS_ENV, raising=False)
    result = HandlerRuntimeInventory().handle(ModelRuntimeInventoryRequest())
    assert result.verdict == "FAIL"
    assert _has(result, "no inventory declared")


def test_runtime_inventory_resolves_from_environment_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(INVENTORY_FILE_ENV, raising=False)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, f"/nonexistent-root:{FIXTURE_ROOT}")
    assert (
        HandlerRuntimeInventory().handle(ModelRuntimeInventoryRequest()).verdict
        == "PASS"
    )


def test_runtime_inventory_resolves_from_file_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OVERLAY_ROOTS_ENV, raising=False)
    monkeypatch.setenv(
        INVENTORY_FILE_ENV, str(FIXTURE_ROOT / OVERLAY_DIR / "inventory.yaml")
    )
    assert (
        HandlerRuntimeInventory().handle(ModelRuntimeInventoryRequest()).verdict
        == "PASS"
    )


def test_runtime_inventory_unreadable_file_fails(tmp_path: Path) -> None:
    result = HandlerRuntimeInventory().handle(
        ModelRuntimeInventoryRequest(inventory_path=str(tmp_path / "absent.yaml"))
    )
    assert result.verdict == "FAIL"
    assert _has(result, "cannot read absent.yaml")


def test_runtime_inventory_cli_prints_pass_and_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--overlay-root", str(FIXTURE_ROOT)]) == 0
    assert capsys.readouterr().out.strip() == "PASS rows=3 exclusions=4 observed=9"


def test_runtime_inventory_cli_fails_nonzero_on_missing_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--inventory", str(tmp_path / "absent.yaml")]) == 1
    assert capsys.readouterr().out.startswith("FAIL")
