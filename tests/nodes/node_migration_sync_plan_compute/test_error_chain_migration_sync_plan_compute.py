# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a request the planner cannot decide is refused, a bad reading is named (OMN-20687)."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.models.migration_sync import (
    ModelMigrationSyncAction,
    ModelMigrationSyncFile,
    ModelMigrationSyncInventory,
)
from omnimarket.nodes.node_migration_sync_plan_compute.handlers import (
    HandlerMigrationSyncPlan,
)
from omnimarket.nodes.node_migration_sync_plan_compute.models import (
    ModelMigrationSyncPlanRequest,
)

EMPTY = ModelMigrationSyncInventory(
    resolved=True,
    nodes_dir="/n",
    dest_root="/d",
    source_files=(),
    vendored_files=(),
    manifest_present=False,
    manifest_rows=(),
)


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelMigrationSyncPlanRequest.model_validate(
            {"mode": "repair", "inventory": EMPTY}
        )


def test_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelMigrationSyncPlanRequest.model_validate(
            {"mode": "check", "inventory": EMPTY, "verbose": True}
        )


@pytest.mark.parametrize("sha", ["", "abc", "G" * 64, "A" * 64])
def test_a_file_reading_without_a_lowercase_sha256_is_refused(sha: str) -> None:
    with pytest.raises(ValidationError):
        ModelMigrationSyncFile(relative_path="n/f.sql", sha256=sha)


@pytest.mark.parametrize(
    "path", ["", "/abs/f.sql", "../f.sql", "n/../../f.sql", "n//f.sql"]
)
def test_an_action_path_that_leaves_the_vendored_tree_is_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        ModelMigrationSyncAction(kind="remove", relative_path=path)


def test_a_copy_action_needs_a_source_path() -> None:
    with pytest.raises(ValidationError, match="source_path"):
        ModelMigrationSyncAction(kind="copy", relative_path="n/f.sql")


def test_an_inventory_that_failed_to_read_is_named_not_decided() -> None:
    failed = EMPTY.model_copy(update={"failure": "dest_root unreadable: [Errno 13]"})
    plan = HandlerMigrationSyncPlan().handle(
        ModelMigrationSyncPlanRequest(mode="write", inventory=failed)
    )
    assert plan.verdict == "inventory_failed"
    assert plan.exit_code == 3
    assert plan.actions == ()
    assert plan.lines[-1].stream == "err"
    assert "dest_root unreadable" in plan.lines[-1].text


def test_an_unresolved_source_is_exit_two_in_both_modes_unless_check_skips_it() -> None:
    unresolved = EMPTY.model_copy(update={"resolved": False, "nodes_dir": ""})
    handler = HandlerMigrationSyncPlan()
    cases: tuple[tuple[Literal["check", "write"], bool, int, str], ...] = (
        ("check", False, 2, "source_unresolvable"),
        ("write", False, 2, "source_unresolvable"),
        ("write", True, 2, "source_unresolvable"),
        ("check", True, 0, "skipped_unresolvable"),
    )
    for mode, skip, code, verdict in cases:
        plan = handler.handle(
            ModelMigrationSyncPlanRequest(
                mode=mode,
                skip_unresolvable=skip,
                inventory=unresolved,
            )
        )
        assert (plan.exit_code, plan.verdict) == (code, verdict)
        assert plan.actions == ()
