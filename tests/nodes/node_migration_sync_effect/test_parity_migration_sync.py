# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity: the read -> plan -> apply chain decides what the old script decided (OMN-20687).

Every case in ``fixtures/parity_cases.json`` was recorded by running the old
``sync-node-migrations.sh`` (the omnibase_infra copy) over a synthetic source tree,
vendored tree and manifest, under ``LC_ALL=C``. It holds the script's stdout, stderr,
interleaved output, exit code and the vendored tree it left behind. The chain below
is run over the same trees, with the three nodes' requests and results handed
between them as JSON the way the bus hands them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.migration_sync import ModelMigrationSyncInventory
from omnimarket.nodes.node_migration_sync_effect.handlers import (
    HandlerMigrationSyncApply,
    HandlerMigrationSyncRead,
)
from omnimarket.nodes.node_migration_sync_effect.models import (
    ModelMigrationSyncApplyRequest,
    ModelMigrationSyncApplyResult,
    ModelMigrationSyncReadRequest,
)
from omnimarket.nodes.node_migration_sync_plan_compute.handlers import (
    HandlerMigrationSyncPlan,
)
from omnimarket.nodes.node_migration_sync_plan_compute.models import (
    ModelMigrationSyncPlanRequest,
    ModelMigrationSyncPlanResult,
)

CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "parity_cases.json").read_text()
)


def run_chain(case: dict[str, Any], root: Path) -> dict[str, Any]:
    """Build the case's trees under ``root``, then read, plan and apply through JSON."""
    nodes = root / "src" / "omnimarket" / "nodes"
    nodes.mkdir(parents=True)
    dest = root / "dest"
    dest.mkdir()
    for rel, content in case["source"].items():
        (nodes / rel).parent.mkdir(parents=True, exist_ok=True)
        (nodes / rel).write_text(content)
    for rel, content in case["vendored"].items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(content)
    manifest = root / "application-migrations.tsv"
    if case["manifest"] is not None:
        manifest.write_text(case["manifest"])

    explicit = case["resolve"] == "explicit"
    read_request = ModelMigrationSyncReadRequest(
        omnimarket_src=str(root) if explicit else "",
        omni_home="",
        allow_installed_package=False,
        dest_root=str(dest),
        manifest_path=str(manifest),
    )
    inventory = HandlerMigrationSyncRead().handle(
        ModelMigrationSyncReadRequest.model_validate_json(
            read_request.model_dump_json()
        )
    )
    plan = HandlerMigrationSyncPlan().handle(
        ModelMigrationSyncPlanRequest.model_validate_json(
            ModelMigrationSyncPlanRequest(
                mode=case["mode"],
                skip_unresolvable=case["skip_unresolvable"],
                inventory=ModelMigrationSyncInventory.model_validate_json(
                    inventory.model_dump_json()
                ),
            ).model_dump_json()
        )
    )
    plan = ModelMigrationSyncPlanResult.model_validate_json(plan.model_dump_json())
    applied = HandlerMigrationSyncApply().handle(
        ModelMigrationSyncApplyRequest.model_validate_json(
            ModelMigrationSyncApplyRequest(
                dest_root=str(dest), actions=plan.actions
            ).model_dump_json()
        )
    )
    assert isinstance(applied, ModelMigrationSyncApplyResult)
    assert applied.failed == ()

    def text(stream: str | None = None) -> list[str]:
        return [
            line.text for line in plan.lines if stream is None or line.stream == stream
        ]

    return {
        "exit_code": plan.exit_code,
        "stdout": text("out"),
        "stderr": text("err"),
        "merged": text(),
        "final_vendored": {
            str(p.relative_to(dest)): p.read_text()
            for p in sorted(dest.rglob("*"))
            if p.is_file()
        },
        "nodes_dir": inventory.nodes_dir,
        "dest_root": str(dest),
    }


def _expected(case: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    def sub(lines: list[str]) -> list[str]:
        return [
            line.replace("{NODES_DIR}", got["nodes_dir"]).replace(
                "{DEST_ROOT}", got["dest_root"]
            )
            for line in lines
        ]

    exp = case["expected"]
    return {
        "exit_code": exp["exit_code"],
        "stdout": sub(exp["stdout"]),
        "stderr": sub(exp["stderr"]),
        "merged": sub(exp["merged"]),
        "final_vendored": exp["final_vendored"],
    }


def test_fixture_covers_both_modes_and_every_exit_code() -> None:
    assert len(CASES) >= 250
    assert {c["mode"] for c in CASES} == {"check", "write"}
    assert {c["expected"]["exit_code"] for c in CASES} == {0, 1, 2}
    assert any("vendored" in line for c in CASES for line in c["expected"]["stdout"])
    assert any(
        "removed stale" in line for c in CASES for line in c["expected"]["stdout"]
    )
    assert any(
        "checksum mismatch" in line for c in CASES for line in c["expected"]["stderr"]
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_chain_decides_what_the_old_script_decided(
    case: dict[str, Any], tmp_path: Path
) -> None:
    got = run_chain(case, tmp_path)
    expected = _expected(case, got)
    assert {k: got[k] for k in expected} == expected
