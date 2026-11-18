# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4: the same merge seen from both merge sources yields one verdict."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    READ_SUFFIX,
    SLACK_SUFFIX,
    VERDICT_SUFFIX,
    make_rig,
    pr_merged_event,
    read_answer,
    repo_merged_event,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("first_is_actions", [True, False])
def test_one_verdict_per_merge_second_source_before_the_read_is_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_is_actions: bool
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    first, second = (
        (pr_merged_event(), repo_merged_event())
        if first_is_actions
        else (repo_merged_event(), pr_merged_event())
    )
    assert rig.handler.handle(first).outcome.value == "read_requested"
    assert rig.handler.handle(second).outcome.value == "duplicate"
    rig.handler.handle(read_answer(rig))
    assert len(rig.publisher.on(READ_SUFFIX)) == 1
    assert len(rig.rows("merge_override_verdicts")) == 1
    assert len(rig.publisher.on(VERDICT_SUFFIX)) == 1
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


@pytest.mark.parametrize("first_is_actions", [True, False])
def test_one_verdict_per_merge_second_source_after_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_is_actions: bool
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    first, second = (
        (pr_merged_event(), repo_merged_event())
        if first_is_actions
        else (repo_merged_event(), pr_merged_event())
    )
    rig.handler.handle(first)
    rig.handler.handle(read_answer(rig))
    assert rig.handler.handle(second).outcome.value == "duplicate"
    assert len(rig.publisher.on(READ_SUFFIX)) == 1
    assert len(rig.rows("merge_override_verdicts")) == 1
    assert len(rig.publisher.on(VERDICT_SUFFIX)) == 1
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_one_verdict_per_merge_two_pull_requests_get_two_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    for pr in (7, 8):
        rig.handler.handle(pr_merged_event(pr))
        rig.handler.handle(read_answer(rig, pr=pr, state=None))
    assert len(rig.rows("merge_override_verdicts")) == 2
    assert len(rig.publisher.on(VERDICT_SUFFIX)) == 2


def test_one_verdict_per_merge_an_unmerged_repo_merged_event_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    event = repo_merged_event().model_copy(update={"state": "closed"})
    assert rig.handler.handle(event).outcome.value == "ignored"
    assert rig.publisher.calls == []
