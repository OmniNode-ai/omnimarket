# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 to AC5 of the ticket hand-edit alarm, each over recorded Linear replies."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.model_ticket_contract_written import ticket_content_sha256
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    CURSOR,
    OPEN_FINDINGS,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditOutcome,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    EDITED_AT,
    FINDING_SUFFIX,
    NODE_ACTOR,
    NOW,
    PERSON,
    SETTINGS,
    SLACK_SUFFIX,
    WRITTEN_AT,
    fingerprint_of,
    history_reply,
    issues_reply,
    make_rig,
    standard_ticket,
    written,
)

pytestmark = pytest.mark.unit


def _differs(finding: dict[str, object]) -> list[str]:
    differs = finding["differs"]
    assert isinstance(differs, list)
    return [str(part) for part in differs]


def _edit_criterion(body: str) -> str:
    """A hand edit of one criterion line above an unchanged block."""
    edited = body.replace(
        "the unit holds for every fixture", "the unit holds for most fixtures", 1
    )
    assert edited != body
    return edited


def test_hand_edit_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AC1: a description that differs from the newest write yields one error and one alarm."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    edited = _edit_criterion(body)
    rig.reader.issues = issues_reply((ticket, title, edited, EDITED_AT))
    rig.reader.history = history_reply(
        (WRITTEN_AT, [NODE_ACTOR]), (EDITED_AT, [PERSON])
    )

    result = rig.sweep()

    findings = rig.publisher.on(FINDING_SUFFIX)
    commands = rig.publisher.on(SLACK_SUFFIX)
    assert result.outcome is EnumTicketHandEditOutcome.EVALUATED
    assert len(findings) == 1
    assert len(commands) == 1
    finding = findings[0]
    assert finding["kind"] == "ticket_hand_edit"
    assert finding["ticket_id"] == ticket
    assert finding["fingerprint_written"] == fingerprint_of(body)
    assert finding["fingerprint_found"] == fingerprint_of(edited)
    assert finding["content_sha256_found"] == ticket_content_sha256(title, edited)
    assert finding["editors"] == [PERSON]
    assert commands[0]["idempotency_key"] == finding["idempotency_key"]
    assert finding["idempotency_key"] == (
        f"ticket_hand_edit:{ticket}:{ticket_content_sha256(title, edited)}"
    )
    assert ticket in str(commands[0]["text"])
    rows = rig.rows(OPEN_FINDINGS)
    assert [row["status"] for row in rows] == ["open"]


def test_hand_edit_raised_for_title_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A title edited by hand, the body untouched, differs from the write record."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title + " (edited)", body, EDITED_AT))

    rig.sweep()

    findings = rig.publisher.on(FINDING_SUFFIX)
    assert len(findings) == 1
    assert findings[0]["fingerprint_written"] == findings[0]["fingerprint_found"]


def test_no_finding_when_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC2: content equal to the newest write yields nothing; one edit alarms once."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title, body, WRITTEN_AT))

    first = rig.sweep()
    assert first.outcome is EnumTicketHandEditOutcome.EVALUATED
    assert first.issues_read == 1
    assert rig.publisher.calls == []

    edited = _edit_criterion(body)
    rig.reader.issues = issues_reply((ticket, title, edited, EDITED_AT))
    rig.sweep()
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1
    # The same edit read again (the cursor rewound by hand) publishes no second alarm.
    rig.database.upsert(CURSOR, "name", {**rig.rows(CURSOR)[0], "position": WRITTEN_AT})
    again = rig.sweep()
    assert again.issues_read == 1
    assert len(again.findings_already_open) == 1
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1


def test_editor_not_node(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AC3: a revision by another actor is ticket_hand_edit naming that actor, content matching."""
    settings = {**SETTINGS, "node_actor_id": NODE_ACTOR}
    rig = make_rig(monkeypatch, tmp_path, settings=settings)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title, body, EDITED_AT))
    rig.reader.history = history_reply(
        (WRITTEN_AT, [NODE_ACTOR]), (EDITED_AT, [PERSON])
    )

    rig.sweep()

    findings = rig.publisher.on(FINDING_SUFFIX)
    assert len(findings) == 1
    assert findings[0]["kind"] == "ticket_hand_edit"
    assert findings[0]["editors"] == [PERSON]
    assert any(PERSON in part for part in _differs(findings[0]))
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1


def test_editor_node_only_is_its_own_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Revisions only by the node's identity are its own write in flight, not a hand edit."""
    settings = {**SETTINGS, "node_actor_id": NODE_ACTOR}
    rig = make_rig(monkeypatch, tmp_path, settings=settings)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title, _edit_criterion(body), EDITED_AT))
    rig.reader.history = history_reply((EDITED_AT, [NODE_ACTOR]))

    rig.sweep()

    assert rig.publisher.calls == []


def test_unreadable_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC4: a failing Linear read is detector_unreadable with an alarm; the cursor holds."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.reader.issues = issues_reply((ticket, title, body, WRITTEN_AT))
    rig.sweep()
    cursor = rig.rows(CURSOR)[0]["position"]
    assert cursor == WRITTEN_AT

    rig.reader.issues_error = "HTTP Error 503"
    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    assert result.cursor_after == cursor
    assert rig.rows(CURSOR)[0]["position"] == cursor
    findings = rig.publisher.on(FINDING_SUFFIX)
    assert [finding["kind"] for finding in findings] == ["detector_unreadable"]
    assert "HTTP Error 503" in str(findings[0]["reason"])
    commands = rig.publisher.on(SLACK_SUFFIX)
    assert len(commands) == 1
    assert commands[0]["idempotency_key"] == findings[0]["idempotency_key"]

    # The next failure over the same cursor is the same open finding, not a second alarm.
    rig.sweep()
    assert len(rig.publisher.on(SLACK_SUFFIX)) == 1
    assert rig.reader.cursors[-1] == cursor


def test_unreadable_store_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unreadable write record is detector_unreadable too, never a quiet skip."""
    rig = make_rig(monkeypatch, tmp_path)

    def broken(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("database down")

    monkeypatch.setattr(rig.database, "query", broken)
    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.UNREADABLE
    assert [f["kind"] for f in rig.publisher.on(FINDING_SUFFIX)] == [
        "detector_unreadable"
    ]
    assert rig.reader.cursors == []


def test_finding_closed_by_node_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC5: a later written event matching the current content closes the open finding."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    edited = _edit_criterion(body)
    rig.reader.issues = issues_reply((ticket, title, edited, EDITED_AT))
    rig.sweep()
    assert [row["status"] for row in rig.rows(OPEN_FINDINGS)] == ["open"]

    # A write record of other content closes nothing.
    other = rig.record(written(ticket, title, body, at=EDITED_AT))
    assert other.findings_closed == ()
    assert [row["status"] for row in rig.rows(OPEN_FINDINGS)] == ["open"]

    # The node's pull adopts the edited text: the finding closes.
    adopted = rig.record(written(ticket, title, edited, at=NOW))
    assert len(adopted.findings_closed) == 1
    assert [row["status"] for row in rig.rows(OPEN_FINDINGS)] == ["closed"]

    # And the next sweep over the adopted text raises nothing new.
    rig.reader.issues = issues_reply((ticket, title, edited, NOW))
    rig.sweep()
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1


def test_older_write_never_replaces_newer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The newest written event per ticket is kept; the newer Linear updatedAt wins."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body, at=NOW))
    stale = rig.record(written(ticket, title, _edit_criterion(body), at=WRITTEN_AT))
    assert stale.outcome is EnumTicketHandEditOutcome.IGNORED


def test_throttled_within_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.sweep()
    rig.clock.advance(-61 + 10)
    assert rig.sweep().outcome is EnumTicketHandEditOutcome.THROTTLED
    assert len(rig.reader.cursors) == 1


def test_non_standard_ticket_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ticket with no contract block and no write record is not this node's."""
    rig = make_rig(monkeypatch, tmp_path)
    rig.reader.issues = issues_reply(("OMN-1", "free text", "no block here", EDITED_AT))
    result = rig.sweep()
    assert result.issues_read == 1
    assert rig.publisher.calls == []


def test_block_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title, "the block is gone", EDITED_AT))
    rig.sweep()
    findings = rig.publisher.on(FINDING_SUFFIX)
    assert len(findings) == 1
    assert findings[0]["fingerprint_found"] is None
    assert any("removed" in part for part in _differs(findings[0]))


def test_missing_overlay_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "absent"))
    assert rig.sweep().outcome is EnumTicketHandEditOutcome.REFUSED
