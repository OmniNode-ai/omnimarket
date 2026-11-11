# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: a merge with no landing receipt and no override row is a finding with one alarm."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
)
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HEAD,
    MERGE_COMMIT,
    PR,
    REPO,
    SLACK_SUFFIX,
    VERDICT_SUFFIX,
    drive_merge,
    make_rig,
    read_answer,
)

pytestmark = pytest.mark.unit
KEY = f"merge-override:{REPO}#{PR}:{HEAD}"
FINDING_TOPIC = "onex.evt.omnimarket.merge-override-finding.v1"


def test_merge_without_override_emits_finding_command_and_open_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    result = drive_merge(rig)
    assert result.verdict is not None
    assert result.verdict.verdict is EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE
    assert result.verdict.head_sha == HEAD
    assert result.verdict.merger == HAND
    assert result.finding_published
    assert result.command_published
    assert not result.error
    [command] = rig.publisher.on(SLACK_SUFFIX)
    assert command["idempotency_key"] == KEY
    assert command["channel"] == "C0TESTCHANNEL"
    text = str(command["text"])
    for fact in (REPO, f"#{PR}", HEAD, MERGE_COMMIT, HAND):
        assert fact in text
    [finding] = rig.publisher.on(FINDING_SUFFIX)
    assert finding["verdict"] == "merge_without_override"
    assert finding["idempotency_key"] == KEY
    assert [t for t, _ in rig.publisher.calls if t.endswith(FINDING_SUFFIX)] == [
        FINDING_TOPIC
    ]
    [open_row] = rig.rows("merge_override_open_findings")
    assert open_row["finding_key"] == KEY
    [verdict_row] = rig.rows("merge_override_verdicts")
    assert verdict_row["verdict"] == "merge_without_override"
    assert verdict_row["receipt_seen"] is False
    assert len(rig.publisher.on(VERDICT_SUFFIX)) == 1


def test_merge_without_override_is_not_repeated_by_a_replayed_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    drive_merge(rig)
    replay = rig.handler.handle(read_answer(rig))
    assert replay.outcome.value == "ignored"
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1
    assert len(rig.rows("merge_override_open_findings")) == 1
