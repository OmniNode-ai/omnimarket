# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the host drift owner dispatch (OMN-24239).

Seam 1: the row this node renders from an open item is the row the alarm owner responder (omnimarket 6037,
node_alarm_owner_responder_compute) reads: it dispatches on the key `<condition>:<host>:<item key>`. Failing
control: a row whose cell name changed (actor, condition or subject) makes the responder dispatch nothing.
Seam 2: the cells of the row follow the lab alarm's own renderer (render_ledger_row of node_lab_alarm_deliver_effect),
so both writers of an alarm row parse to the same cells.
Seam 3: the runner command carries the flags the remote-lane runner's `run` takes.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_alarm_owner_responder_compute import (
    HandlerAlarmOwnerResponder,
    ModelAlarmOwnerRequest,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.handlers.handler_host_drift_owner_dispatch import (
    cause_key_of,
    render_drift_row,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOwnerRequest,
)

from .support import NOW, item, overlay, rig

pytestmark = pytest.mark.unit

NOW_STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ask(rows: tuple[str, ...]):  # type: ignore[no-untyped-def]
    return HandlerAlarmOwnerResponder().handle(
        ModelAlarmOwnerRequest(rows=rows, now=NOW_STAMP, policy=overlay().policy)
    )


def test_the_rendered_row_is_dispatched_on_its_cause_key() -> None:
    row = render_drift_row(item(), overlay().alarm)
    answer = _ask((row,))
    assert [d.cause_key for d in answer.dispatches] == [
        cause_key_of(item(), overlay().alarm)
    ]
    assert answer.alarm_rows == 1
    assert answer.refusals == ()


def test_a_changed_cell_name_dispatches_nothing() -> None:
    row = render_drift_row(item(), overlay().alarm)
    for old, new in (
        ("actor=", "owner="),
        ("condition=", "cond="),
        ("subject=", "target="),
    ):
        changed = row.replace(old, new, 1)
        answer = _ask((changed,))
        assert answer.dispatches == (), old


def test_the_row_cells_are_the_lab_alarms_cells() -> None:
    parsed = parse_row(render_drift_row(item(), overlay().alarm))
    assert parsed is not None
    assert parsed.row_type == "FRICTION"
    assert parsed.stamp == item().first_seen_at
    for cell in (
        "lane",
        "actor",
        "model",
        "class",
        "condition",
        "subject",
        "raised_at",
        "existing",
        "detail",
        "evidence",
    ):
        assert parsed.value(cell), cell


def test_a_pipe_or_line_break_in_the_observed_text_stays_one_cell() -> None:
    text = "a|b\nc | d"
    parsed = parse_row(render_drift_row(item(observed=text), overlay().alarm))
    assert parsed is not None
    assert parsed.value("evidence") == "a b c d"


def test_the_flags_the_command_carries_are_the_runner_run_verbs_flags() -> None:
    """The runner script lives in the plugin checkout, so its flags are pinned here.

    Read on the lab 2026-11-13 from `onex_remote_lane.py run --help`: --brief, --lane, --model, --ticket, --cause,
    --parent and --priority-class are all flags of the `run` verb. A rename on either side shows in this set.
    """
    result = rig((item(),)).handler.handle(ModelDriftOwnerRequest(dry_run=True))
    flags = {a for a in result.lanes[0].argv if a.startswith("--")}
    assert flags == {
        "--brief",
        "--lane",
        "--model",
        "--ticket",
        "--cause",
        "--parent",
        "--priority-class",
    }
