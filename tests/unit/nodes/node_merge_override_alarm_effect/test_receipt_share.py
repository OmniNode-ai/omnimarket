# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam reading: the share of merges that had a landing receipt, per merge source."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.receipt_share import (
    receipt_share,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.store_merge_override import (
    MergeOverrideStore,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    arm_receipt,
    drive_merge,
    make_rig,
    pr_merged_event,
    read_answer,
    repo_merged_event,
)

pytestmark = pytest.mark.unit


def test_receipt_share_counts_per_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.handler.handle(arm_receipt(pr=7))
    drive_merge(rig, pr=7)  # actions source, with a receipt
    rig.handler.handle(repo_merged_event(pr=8))  # watcher source, no receipt
    rig.handler.handle(read_answer(rig, pr=8))
    share = receipt_share(MergeOverrideStore(rig.database).verdict_rows())
    assert share.per_source == {"pr-merged": (1, 1), "repo-merged": (1, 0)}
    assert (share.total, share.with_receipt) == (2, 1)


def test_receipt_share_of_nothing_is_zero() -> None:
    share = receipt_share([])
    assert (share.total, share.with_receipt, share.per_source) == (0, 0, {})
    assert pr_merged_event().pr_number == 7
