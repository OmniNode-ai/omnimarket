# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a ticket equal to its newest write record yields nothing, and one edit alarms once."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditOutcome,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    NODE_ACTOR,
    SLACK_SUFFIX,
    WRITTEN_BODY,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def test_no_finding_when_matches_the_newest_write_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(WRITTEN_BODY)], [revision(20, NODE_ACTOR)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.SWEPT
    assert result.checked == 1
    assert result.findings == ()
    assert rig.publisher.calls == []


def test_no_finding_when_matches_a_later_record_after_an_older_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], [revision(20, NODE_ACTOR)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written(minutes_ago=30))
    rig.handler.handle(written(HAND_EDITED_BODY, minutes_ago=6))
    rig.handler.handle(
        written(minutes_ago=40)
    )  # an older arrival never replaces the newest

    rig.sweep()

    assert rig.publisher.calls == []


def test_no_finding_when_matches_a_second_run_over_the_same_edit_publishes_no_second_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()
    # A reader that returns the issue again (the cursor missed it, or Linear re-listed it).
    rig.store.set_cursor(rig.clock_datetime_minus(600), rig.clock_datetime())
    second = rig.sweep(advance_seconds=61)

    assert second.findings == ()
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_no_finding_when_matches_the_cursor_moves_past_what_was_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(WRITTEN_BODY, updated_minutes_ago=5)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()
    rig.sweep(advance_seconds=61)

    assert rig.store.cursor() == source.issues[0].updated_at
    assert source.list_calls[1] == source.issues[0].updated_at
