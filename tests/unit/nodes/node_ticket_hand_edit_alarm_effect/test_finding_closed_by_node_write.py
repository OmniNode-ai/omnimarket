# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5: a later written event that matches the current content closes the open finding."""

from __future__ import annotations

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
    TICKET,
    WRITTEN_BODY,
    Rig,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def _open_finding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    source = Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())
    rig.sweep()
    assert len(rig.store.open_findings_for(TICKET)) == 1
    return rig


def test_finding_closed_by_node_write_that_adopts_the_found_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = _open_finding(monkeypatch, tmp_path)

    result = rig.handler.handle(written(HAND_EDITED_BODY, minutes_ago=4))

    assert result.outcome is EnumTicketHandEditOutcome.WRITE_RECORDED
    assert result.closed == 1
    assert rig.store.open_findings_for(TICKET) == []
    [row] = rig.rows(OPEN_FINDINGS)
    assert row["closed_at"] is not None
    assert "written event" in str(row["closed_by"])


def test_finding_closed_by_node_write_is_not_closed_by_a_write_of_other_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = _open_finding(monkeypatch, tmp_path)

    result = rig.handler.handle(written(WRITTEN_BODY, minutes_ago=4))

    assert result.closed == 0
    assert len(rig.store.open_findings_for(TICKET)) == 1


def test_finding_closed_by_node_write_is_not_closed_by_a_stale_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = _open_finding(monkeypatch, tmp_path)

    result = rig.handler.handle(written(HAND_EDITED_BODY, minutes_ago=40))

    assert result.outcome is EnumTicketHandEditOutcome.STALE_WRITE_IGNORED
    assert len(rig.store.open_findings_for(TICKET)) == 1


def test_finding_closed_by_node_write_a_sync_over_the_edit_closes_it_on_the_next_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = _open_finding(monkeypatch, tmp_path)
    # The lane syncs its own version over the edit: the node writes, Linear returns it.
    rig.source.issues = [issue(WRITTEN_BODY, updated_minutes_ago=2)]
    rig.source.revisions = [revision(2, "example-node-actor")]
    rig.handler.handle(written(WRITTEN_BODY, minutes_ago=2))

    result = rig.sweep(advance_seconds=61)

    assert result.closed == 1
    assert rig.store.open_findings_for(TICKET) == []
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1
