# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sweep reads Linear at most once per interval and judges an issue only after its write's event can have arrived."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    EnumTicketHandEditOutcome,
    ModelTicketHandEditSettings,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit


def test_sweep_cadence_reads_linear_once_per_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source()
    rig = make_rig(monkeypatch, tmp_path, source=source)

    assert rig.sweep().outcome is EnumTicketHandEditOutcome.SWEPT
    assert rig.sweep(advance_seconds=59).outcome is EnumTicketHandEditOutcome.THROTTLED
    assert len(source.list_calls) == 1
    assert rig.sweep(advance_seconds=2).outcome is EnumTicketHandEditOutcome.SWEPT
    assert len(source.list_calls) == 2


def test_sweep_cadence_the_interval_defaults_to_sixty_seconds() -> None:
    assert ModelTicketHandEditSettings().check_interval_seconds == 60


def test_sweep_cadence_waits_out_the_grace_window_before_judging_an_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source(
        [issue(HAND_EDITED_BODY, updated_minutes_ago=0.2)], [revision(0.2, HAND)]
    )
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    young = rig.sweep()
    assert young.checked == 0
    assert rig.publisher.on(FINDING_SUFFIX) == []
    cursor = rig.store.cursor()
    assert cursor is not None
    assert cursor < source.issues[0].updated_at

    settled = rig.sweep(advance_seconds=61)
    assert settled.checked == 1
    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1


def test_sweep_cadence_a_write_event_that_arrives_after_the_edit_is_not_a_hand_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The node writes, Linear updates, the sweep runs, and the written event is still in flight."""
    source = Source([issue(HAND_EDITED_BODY, updated_minutes_ago=0.2)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    rig.handler.handle(written())

    rig.sweep()
    rig.handler.handle(written(HAND_EDITED_BODY, minutes_ago=0.2))
    rig.sweep(advance_seconds=61)

    assert rig.publisher.on(FINDING_SUFFIX) == []
