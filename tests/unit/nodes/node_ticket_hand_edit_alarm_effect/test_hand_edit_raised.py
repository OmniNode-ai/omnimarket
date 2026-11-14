# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: a ticket that differs from the newest write record is one ticket_hand_edit with one alarm."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.compare_ticket import (
    content_sha256,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    OPEN_FINDINGS,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditKind,
    EnumTicketHandEditOutcome,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    SLACK_SUFFIX,
    TICKET,
    TITLE,
    WRITTEN_BODY,
    WRITTEN_FINGERPRINT,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def test_hand_edit_raised_names_both_fingerprints_and_shares_one_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(HAND_EDITED_BODY)],
        [revision(20, "example-node-actor"), revision(10, HAND)],
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    result = rig.sweep()

    assert result.outcome is EnumTicketHandEditOutcome.SWEPT
    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["kind"] == EnumTicketHandEditKind.TICKET_HAND_EDIT.value
    assert event["ticket_id"] == TICKET
    assert event["written_fingerprint"] == WRITTEN_FINGERPRINT
    found = event["found_fingerprint"]
    assert found not in ("", "none", WRITTEN_FINGERPRINT)
    assert event["found_content_sha256"] == content_sha256(TITLE, HAND_EDITED_BODY)
    assert event["editors"] == [HAND]
    assert "contract_not_synced" in str(event["differences"])
    [command] = rig.publisher.on(SLACK_SUFFIX)
    assert command["idempotency_key"] == event["idempotency_key"]
    assert (
        event["idempotency_key"]
        == f"{TICKET}:{content_sha256(TITLE, HAND_EDITED_BODY)}"
    )
    assert WRITTEN_FINGERPRINT in str(command["text"])
    assert str(found) in str(command["text"])
    [row] = rig.rows(OPEN_FINDINGS)
    assert row["finding_key"] == event["idempotency_key"]
    assert row["closed_at"] is None


def test_hand_edit_raised_when_the_block_was_removed_from_a_written_ticket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = WRITTEN_BODY.split("## Contract")[0].rstrip() + "\n"
    rig = make_rig(
        monkeypatch, tmp_path, source=Source([issue(body)], [revision(10, HAND)])
    )
    rig.handler.handle(written())

    rig.sweep()

    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["found_fingerprint"] == "none"
    assert "block was removed" in str(event["differences"])


def test_hand_edit_raised_without_a_write_record_when_the_block_does_not_recompute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(
        monkeypatch,
        tmp_path,
        source=Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)]),
    )

    rig.sweep()

    [event] = rig.publisher.on(FINDING_SUFFIX)
    assert event["written_fingerprint"] == ""
    assert event["editors"] == [HAND]
    assert rig.source.history_calls == 1


def test_hand_edit_raised_ignores_a_ticket_that_is_not_a_standard_ticket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, source=Source([issue("free text, no block")]))

    result = rig.sweep()

    assert result.checked == 1
    assert rig.publisher.calls == []
