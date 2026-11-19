# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-removal recheck reads the ledger once per run, never once per tree.

On the orchestrating Mac the ledger with its rolled files is about 260 MB; reading it for every
tree about to be removed made a run's cost grow with its tree count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.rules_ledger_claims import ModelLedgerRead
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import adapter_facts
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
)
from tests.test_worktree_reconcile_effect import NO_UNITS, command

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


class CountingLedgerReader:
    """A ledger source that counts its reads and always answers a fresh, empty ledger."""

    def __init__(self) -> None:
        self.reads = 0

    def read(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> ModelLedgerRead | None:
        self.reads += 1
        return ModelLedgerRead(fresh=True)


def _trees(tmp_path: Path, count: int) -> Path:
    root = tmp_path / "registry" / "trees"
    for index in range(count):
        (root / f"task-{index}").mkdir(parents=True)
    return root


def test_a_run_reads_the_ledger_at_discovery_and_once_more_for_all_its_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    reader = CountingLedgerReader()
    probe = GitWorktreeFactsProbe(unit_reader=NO_UNITS, ledger_reader=reader)
    trees = probe.discover(command(roots=(str(_trees(tmp_path, 3)),)), NOW)
    assert len(trees) == 3
    assert reader.reads == 1
    for tree in trees:
        assert probe.revalidate(tree, NOW).ledger_known
    # Three trees were rechecked; the ledger was read once for all of them.
    assert reader.reads == 2


def test_the_next_run_reads_afresh_and_is_not_served_the_previous_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_facts, "process_cwds", lambda: ())
    reader = CountingLedgerReader()
    probe = GitWorktreeFactsProbe(unit_reader=NO_UNITS, ledger_reader=reader)
    cmd = command(roots=(str(_trees(tmp_path, 2)),))
    for _ in range(2):
        for tree in probe.discover(cmd, NOW):
            probe.revalidate(tree, NOW)
    assert reader.reads == 4
