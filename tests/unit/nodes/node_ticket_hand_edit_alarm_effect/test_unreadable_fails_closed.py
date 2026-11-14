# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4: an unreadable source is detector_unreadable with an alarm, and the cursor stays."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    OPEN_FINDINGS,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditOutcome,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    NOW,
    SLACK_SUFFIX,
    WRITTEN_BODY,
    Publisher,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def test_unreadable_fails_closed_on_a_failing_linear_list_and_leaves_the_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(list_error="Linear answered 503")
    rig = make_rig(monkeypatch, tmp_path, source=source)
    before = NOW - timedelta(minutes=2)
    rig.store.set_cursor(before, NOW)

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    assert rig.store.cursor() == before
    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["kind"] == "detector_unreadable"
    assert "linear_issue_list" in str(event["reason"])
    assert "503" in str(event["reason"])
    [command] = rig.publisher.on(SLACK_SUFFIX)
    assert command["idempotency_key"] == event["idempotency_key"]
    assert [r["kind"] for r in rig.rows(OPEN_FINDINGS)] == ["detector_unreadable"]


def test_unreadable_fails_closed_without_a_cursor_never_creates_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, source=Source(list_error="timeout"))

    rig.sweep()

    assert rig.store.cursor() is None


def test_unreadable_fails_closed_on_a_failing_history_read_and_judges_nothing_past_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], history_error="history refused")
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    assert rig.store.cursor() is None
    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert "linear_description_history" in str(event["reason"])


def test_unreadable_fails_closed_when_the_write_record_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, source=Source([issue(WRITTEN_BODY)]))

    def refuse(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(rig.database, "query", refuse)

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert "write_record_store" in str(event["reason"])


def test_unreadable_fails_closed_alarms_once_until_a_sweep_reads_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(list_error="Linear answered 503")
    rig = make_rig(monkeypatch, tmp_path, source=source)

    rig.sweep()
    rig.sweep(advance_seconds=61)
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1

    source.list_error = None
    clean = rig.sweep(advance_seconds=61)
    assert clean.outcome is EnumTicketHandEditOutcome.SWEPT
    assert [r["closed_at"] is not None for r in rig.rows(OPEN_FINDINGS)] == [True]

    source.list_error = "Linear answered 503 again"
    rig.sweep(advance_seconds=61)
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 2


def test_unreadable_fails_closed_keeps_the_cursor_when_an_alarm_cannot_be_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)])
    rig = make_rig(
        monkeypatch, tmp_path, source=source, publisher=Publisher(error="broker down")
    )
    rig.handler.handle(written())

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    assert rig.store.cursor() is None
    # No ticket_hand_edit row: the open row is written last, so the next sweep raises it again.
    assert [r["kind"] for r in rig.rows(OPEN_FINDINGS)] == ["detector_unreadable"]
