# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate-fix guard (OMN-17427) reads the PR watcher, the ledger and git; these suites test other verbs."""

from __future__ import annotations

import pytest

from omnimarket.handlers.scm import scm_cli
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers import (
    handler_pr_duplicate_snapshot,
)
from tests.helpers.fake_ledger_rows import FakeLedgerRows, quiet_heartbeat_row


@pytest.fixture(autouse=True)
def _pr_create_finds_no_duplicate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every scm suite but the guard's own runs ``pr-create`` with the duplicate check answered clear."""
    if "duplicate_guard" in request.module.__name__:
        return
    monkeypatch.setattr(scm_cli, "refuse_or_continue", lambda **_: None)


@pytest.fixture(autouse=True)
def ledger_rows(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> FakeLedgerRows:
    """The guard's ledger read goes to this fake: the CLI builds its default reader, which the guard suites replace."""
    rows = FakeLedgerRows()
    if "duplicate_guard" in request.module.__name__:
        # A live ledger always has a row in its last hour; the claims read refuses an answer with none (it is not a read of the
        # ledger), so the default ledger holds one quiet lane's heartbeat and no claim.
        rows.lines = [quiet_heartbeat_row()]
        monkeypatch.setattr(
            handler_pr_duplicate_snapshot,
            "SubprocessLedgerRowsReader",
            lambda _environ, **_kwargs: rows,
        )
    return rows
