# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal check's stale-scope check: the goal_scope overlay's ruling date against the newest goal RULING row.

``handler_goal_scope_stale`` is pure: it is handed the overlay text and the ledger text. It prints one
``MISSING goal-scope-stale`` line when the overlay's ``ruling_date`` is older than the newest ``RULING``
row carrying the typed cell ``kind=goal``, and nothing when the dates are equal or the overlay is newer.
An input it could not read never prints as fine: it prints ``UNKNOWN goal-scope-stale`` (an unreadable or
missing ruling_date, an empty ledger, a goal RULING row whose timestamp cannot be read) or, when the ledger
holds no goal RULING row, a ``NOTE goal-scope-stale`` line.

Every test that expects no MISSING line also asserts the positive control: the same ledger with an older
overlay prints the line, so a check that prints nothing at all cannot pass.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_throughput_goal_check_compute.handlers.handler_goal_scope_stale import (
    HandlerGoalScopeStale,
    ledger_row_is_goal_ruling,
    newest_goal_ruling_ts,
    overlay_ruling_date,
    ruling_date_is_older,
)
from omnimarket.nodes.node_throughput_goal_check_compute.models.model_goal_scope_stale import (
    ModelGoalScopeStaleRequest,
    ModelGoalScopeStaleResult,
)

pytestmark = pytest.mark.unit

NEWEST_GOAL_TS = "2026-11-21T12:52:40Z"

LEDGER = "\n".join(
    [
        "2026-11-05T12:36:07Z | RULING | lane=orchestrator | ticket=OMN-19998 | kind=goal | the 2026-11-05 goal: landing works for the work we are keeping",
        "2026-11-10T08:30:00Z | RULING | lane=orchestrator | ticket=OMN-20000 | kind=other | a ruling about something else",
        "2026-11-12T09:00:00Z | MSG | to=landing-controller | kind=goal | a message, not a ruling",
        f"{NEWEST_GOAL_TS} | RULING | lane=orchestrator | ticket=OMN-26522 | kind=goal | the operator's newest goal",
        "2026-11-22T09:00:00Z | RULING | lane=orchestrator | ticket=OMN-26600 | kind=other | a newer ruling that is not a goal",
        "2026-11-22T10:00:00Z | MSG | to=operator | kind=goal | a newer goal message that is not a ruling",
        "2026-11-22T11:00:00Z | RULING | lane=orchestrator | kind=goals | a near miss of the typed cell",
        "2026-11-22T12:00:00Z | RULING | lane=orchestrator | kind=goal-x | another near miss of the typed cell",
    ]
)

STALE_OVERLAY = "ruling_date: 2026-11-05T12:36:07Z\n"
STALE_LINE = (
    "MISSING goal-scope-stale | overlay_ruling=2026-11-05T12:36:07Z "
    f"newest_goal_ruling={NEWEST_GOAL_TS}"
)


def _check(overlay: str, ledger: str = LEDGER) -> list[str]:
    result = HandlerGoalScopeStale().handle(
        ModelGoalScopeStaleRequest(overlay_text=overlay, ledger_text=ledger)
    )
    assert isinstance(result, ModelGoalScopeStaleResult)
    return list(result.lines)


def _assert_control_prints_the_line() -> None:
    """The positive control: an overlay older than the ruling prints the MISSING line on the fixture ledger."""
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_by_stamp_prints_one_missing_line() -> None:
    assert _check("ruling_date: 2026-11-21T12:52:39Z\n") == [
        f"MISSING goal-scope-stale | overlay_ruling=2026-11-21T12:52:39Z newest_goal_ruling={NEWEST_GOAL_TS}"
    ]
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_by_day_prints_the_day_as_typed() -> None:
    assert _check("ruling_date: 2026-11-20\n") == [
        f"MISSING goal-scope-stale | overlay_ruling=2026-11-20 newest_goal_ruling={NEWEST_GOAL_TS}"
    ]


def test_goal_scope_stale_equal_stamp_prints_no_line() -> None:
    assert _check(f"ruling_date: {NEWEST_GOAL_TS}\n") == []
    _assert_control_prints_the_line()


def test_goal_scope_stale_equal_day_prints_no_line() -> None:
    # a bare day compares as that UTC day against the UTC day of the ruling: the same day is not stale
    assert _check("ruling_date: 2026-11-21\n") == []
    _assert_control_prints_the_line()
    assert _check("ruling_date: 2026-11-20\n") != []


def test_goal_scope_stale_overlay_newer_prints_no_line() -> None:
    assert _check("ruling_date: 2026-11-21T12:52:41Z\n") == []
    assert _check("ruling_date: 2026-11-22\n") == []
    _assert_control_prints_the_line()


def test_goal_scope_stale_stamp_compares_instants_not_text() -> None:
    ledger = (
        "2026-11-21T23:30:00Z | RULING | lane=a | kind=goal | late in the UTC day\n"
    )
    # same UTC day, earlier instant: stale as a stamp, fine as a day
    assert _check("ruling_date: 2026-11-21T01:00:00Z\n", ledger) == [
        "MISSING goal-scope-stale | overlay_ruling=2026-11-21T01:00:00Z newest_goal_ruling=2026-11-21T23:30:00Z"
    ]
    assert _check("ruling_date: 2026-11-21\n", ledger) == []


def test_goal_scope_stale_ignores_a_newer_non_goal_ruling_row() -> None:
    # LEDGER holds newer RULING rows without the typed cell kind=goal (kind=other, kind=goals, kind=goal-x)
    assert _check(f"ruling_date: {NEWEST_GOAL_TS}\n") == []
    # control: the same newer row WITH the typed cell is the newest goal ruling, and the overlay is then stale
    newer_goal = (
        LEDGER
        + "\n2026-11-22T13:00:00Z | RULING | lane=orchestrator | kind=goal | a newer goal\n"
    )
    assert _check(f"ruling_date: {NEWEST_GOAL_TS}\n", newer_goal) == [
        f"MISSING goal-scope-stale | overlay_ruling={NEWEST_GOAL_TS} newest_goal_ruling=2026-11-22T13:00:00Z"
    ]
    _assert_control_prints_the_line()


def test_goal_scope_stale_ignores_a_goal_row_that_is_a_msg_not_a_ruling() -> None:
    ledger = "\n".join(
        [
            "2026-11-05T12:36:07Z | RULING | lane=orchestrator | kind=goal | the goal",
            "2026-11-23T09:00:00Z | MSG | to=operator | kind=goal | a message carrying the cell",
            "2026-11-23T09:30:00Z | STATUS | lane=x | kind=goal | a status carrying the cell",
        ]
    )
    assert _check("ruling_date: 2026-11-05T12:36:07Z\n", ledger) == []
    # control: make the newest row a RULING and the overlay turns stale
    as_ruling = ledger.replace(
        "2026-11-23T09:00:00Z | MSG |", "2026-11-23T09:00:00Z | RULING |"
    )
    assert _check("ruling_date: 2026-11-05T12:36:07Z\n", as_ruling) == [
        "MISSING goal-scope-stale | overlay_ruling=2026-11-05T12:36:07Z newest_goal_ruling=2026-11-23T09:00:00Z"
    ]


def test_goal_scope_stale_newest_goal_ruling_is_by_timestamp_not_row_order() -> None:
    ledger = "\n".join(
        [
            "2026-11-21T12:52:40Z | RULING | lane=a | kind=goal | newest, written first",
            "2026-11-05T12:36:07Z | RULING | lane=a | kind=goal | older, written last",
        ]
    )
    assert newest_goal_ruling_ts(ledger) == (NEWEST_GOAL_TS, 0)
    assert _check("ruling_date: 2026-11-20\n", ledger) != []


def test_goal_scope_stale_row_predicate_needs_the_ruling_type_and_the_exact_cell() -> (
    None
):
    assert ledger_row_is_goal_ruling(
        "2026-11-21T12:52:40Z | RULING | lane=a | kind=goal | t"
    )
    assert not ledger_row_is_goal_ruling(
        "2026-11-21T12:52:40Z | MSG | lane=a | kind=goal | t"
    )
    assert not ledger_row_is_goal_ruling(
        "2026-11-21T12:52:40Z | RULING | lane=a | kind=goals | t"
    )
    assert not ledger_row_is_goal_ruling(
        "2026-11-21T12:52:40Z | RULING | lane=a | xkind=goal | t"
    )
    assert not ledger_row_is_goal_ruling("2026-11-21T12:52:40Z | RULING | lane=a | t")
    assert not ledger_row_is_goal_ruling("")
    assert not ledger_row_is_goal_ruling("not a row")


@pytest.mark.parametrize(
    "overlay",
    [
        'overlay_version: "1.0.0"\nticket: OMN-19998\n',  # no ruling_date key
        "# ruling_date: 2026-01-01\n",  # only inside a comment
        "ruling_date:\n",  # key without a value
        "ruling_date: not-a-date\n",
        "ruling_date: 2026-13-45\n",  # a day that does not exist
        "ruling_date: 2026-11-21T25:00:00Z\n",  # an instant that does not exist
        "ruling_date: 2026-11-21T12:52:40\n",  # no Z: not a UTC stamp
        "ruling_date: 2026-11-05\nruling_date: 2026-11-21\n",  # two answers: neither is trusted
        "",
    ],
)
def test_goal_scope_stale_unreadable_ruling_date_is_unknown_never_fine(
    overlay: str,
) -> None:
    lines = _check(overlay)
    assert len(lines) == 1
    assert lines[0].startswith("UNKNOWN goal-scope-stale | ")
    assert not any(line.startswith("MISSING") for line in lines)
    # control: a readable overlay older than the ruling prints the MISSING line on the same ledger
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_no_goal_ruling_row_is_a_note_not_a_missing_line() -> None:
    ledger = "\n".join(
        [
            "2026-11-10T08:30:00Z | RULING | lane=a | kind=other | not a goal",
            "2026-11-12T09:00:00Z | MSG | to=b | kind=goal | a message",
        ]
    )
    assert _check(STALE_OVERLAY, ledger) == [
        "NOTE goal-scope-stale | no goal RULING row on the ledger"
    ]
    # control: with a goal RULING row the same overlay prints the MISSING line, and no NOTE
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_empty_ledger_is_unknown_not_a_note() -> None:
    for blank in ("", "\n\n", "   \n"):
        lines = _check(STALE_OVERLAY, blank)
        assert len(lines) == 1
        assert lines[0].startswith("UNKNOWN goal-scope-stale | ")
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_goal_ruling_with_an_unreadable_timestamp_is_unknown() -> None:
    ledger = (
        LEDGER
        + "\nyesterday | RULING | lane=a | kind=goal | its timestamp cannot be ordered\n"
    )
    lines = _check(STALE_OVERLAY, ledger)
    assert STALE_LINE in lines
    unknown = [line for line in lines if line.startswith("UNKNOWN goal-scope-stale | ")]
    assert len(unknown) == 1
    assert "1 goal RULING row" in unknown[0]
    # only unreadable goal rows: no MISSING line (nothing to compare), but still not silent
    only_bad = "yesterday | RULING | lane=a | kind=goal | x\n"
    bad_lines = _check(STALE_OVERLAY, only_bad)
    assert [line.split(" | ")[0] for line in bad_lines] == ["UNKNOWN goal-scope-stale"]
    # control
    assert _check(STALE_OVERLAY) == [STALE_LINE]


def test_goal_scope_stale_both_inputs_unread_print_both_unknowns() -> None:
    lines = _check("ticket: OMN-1\n", "")
    assert len(lines) == 2
    assert all(line.startswith("UNKNOWN goal-scope-stale | ") for line in lines)


def test_goal_scope_stale_reads_the_overlay_shape_of_the_internal_plugin() -> None:
    # the shape of plugins/onex-overlays/overlays/goal_scope/overlay.yaml: comment lines above the keys,
    # a quoted ruling sentence holding a '#'-free text, lists, and the ruling_date key the check reads
    overlay = "\n".join(
        [
            "# Overlay for the landing goal's scope (OMN-19998).",
            "#",
            "# Read by the throughput tick's goal check and lab fill.",
            "# ruling_date: 2026-01-01   (a commented-out line is not read)",
            'overlay_version: "1.0.0"',
            "ticket: OMN-19998",
            'ruling: "operator goal 2026-11-05 about 12:40Z: landing works for the work we are keeping"',
            "# The date of the newest operator /goal RULING this scope was last set against.",
            "ruling_date: 2026-11-21T12:52:40Z   # newest goal RULING",
            "in_scope_repos:",
            "  - omnimarket",
            "in_scope_prs: []",
            "paused_prs: []",
            "",
        ]
    )
    assert overlay_ruling_date(overlay) == "2026-11-21T12:52:40Z"
    assert _check(overlay) == []
    assert _check(
        overlay.replace("2026-11-21T12:52:40Z", "2026-11-05T12:36:07Z", 1)
    ) == [
        "MISSING goal-scope-stale | overlay_ruling=2026-11-05T12:36:07Z "
        f"newest_goal_ruling={NEWEST_GOAL_TS}"
    ]
    # quoted scalars read as typed
    assert overlay_ruling_date('ruling_date: "2026-11-21"\n') == "2026-11-21"
    assert overlay_ruling_date("ruling_date: '2026-11-21'\n") == "2026-11-21"
    _assert_control_prints_the_line()


def test_goal_scope_stale_ruling_date_is_older_compares_stamps_and_days() -> None:
    assert ruling_date_is_older("2026-11-21T12:52:39Z", NEWEST_GOAL_TS) is True
    assert ruling_date_is_older(NEWEST_GOAL_TS, NEWEST_GOAL_TS) is False
    assert ruling_date_is_older("2026-11-21T12:52:41Z", NEWEST_GOAL_TS) is False
    assert ruling_date_is_older("2026-11-20", NEWEST_GOAL_TS) is True
    assert ruling_date_is_older("2026-11-21", NEWEST_GOAL_TS) is False
    assert ruling_date_is_older("2026-11-22", NEWEST_GOAL_TS) is False
    assert ruling_date_is_older("garbage", NEWEST_GOAL_TS) is None


def test_goal_scope_stale_request_and_result_are_frozen_and_closed() -> None:
    request = ModelGoalScopeStaleRequest(overlay_text="a", ledger_text="b")
    field_name = "overlay_text"
    with pytest.raises(ValidationError):
        setattr(request, field_name, "c")
    with pytest.raises(ValidationError):
        ModelGoalScopeStaleRequest.model_validate(
            {"overlay_text": "a", "ledger_text": "b", "extra": "x"}
        )
    assert ModelGoalScopeStaleResult().lines == []
