# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the open-obligations RENDERERS (OMN-17019 DoD item 3).

The point of these tests is not that markdown formats correctly. It is that the
markdown is DISPOSABLE: delete it, regenerate it from the projection, and get
the same bytes back. That property is what demotes ``session-goal.md``, the
rolling plan and the session-close open-ask list from stores to views, and it is
this ticket's own falsifiable done-proof in miniature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationState,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_open_obligation_view import (
    ModelOpenObligationView,
)
from omnimarket.nodes.node_projection_open_obligations.obligations_view import (
    open_obligations,
    parse_open_obligations,
    render_delivery_acknowledgements,
    render_open_obligations,
    rows_from_records,
    unmet_obligations_for_close,
)

pytestmark = pytest.mark.unit

_T = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


def _view(**overrides: object) -> ModelOpenObligationView:
    payload: dict[str, object] = {
        "obligation_id": "ob-1",
        "state": EnumObligationState.OPEN,
        "last_event_at": _T,
        "owed_by": "session-abc",
        "asked_by": "operator",
        "acceptance_condition": "brief delivered and acknowledged",
    }
    payload.update(overrides)
    return ModelOpenObligationView(**payload)  # type: ignore[arg-type]


def test_open_set_excludes_every_closed_state() -> None:
    rows = [
        _view(obligation_id="open-1"),
        _view(obligation_id="sat-1", state=EnumObligationState.SATISFIED),
        _view(obligation_id="sup-1", state=EnumObligationState.SUPERSEDED),
        _view(obligation_id="aba-1", state=EnumObligationState.ABANDONED),
    ]
    assert [row.obligation_id for row in open_obligations(rows)] == ["open-1"]


def test_render_parse_round_trips_the_open_set() -> None:
    """The done-proof shape: regenerate from the projection, diff, get zero."""
    rows = [
        _view(obligation_id="ob-1"),
        _view(obligation_id="ob-2", last_event_at=_T + timedelta(hours=1)),
        _view(obligation_id="ob-3", state=EnumObligationState.SATISFIED),
    ]
    rendered = render_open_obligations(rows)
    reparsed = parse_open_obligations(rendered)
    assert render_open_obligations(reparsed) == rendered
    assert [row.obligation_id for row in reparsed] == ["ob-2", "ob-1"]


def test_render_is_deterministic_for_a_tied_timestamp() -> None:
    """Two obligations sharing a timestamp must not swap places between runs.

    Without the ``obligation_id`` tiebreak the regenerate-and-diff proof would
    be flaky, and a flaky proof cannot falsify anything.
    """
    rows = [_view(obligation_id=f"ob-{i}") for i in range(6)]
    assert render_open_obligations(rows) == render_open_obligations(
        list(reversed(rows))
    )


def test_pipes_and_newlines_survive_the_round_trip() -> None:
    """A summary containing a table delimiter must not corrupt the view."""
    row = _view(acceptance_condition="deliver a | b\nand say so")
    reparsed = parse_open_obligations(render_open_obligations([row]))
    assert reparsed[0].acceptance_condition == "deliver a | b and say so"


def test_absent_values_round_trip_as_none_not_as_empty_string() -> None:
    reparsed = parse_open_obligations(
        render_open_obligations([_view(owed_by=None, asked_by=None)])
    )
    assert reparsed[0].owed_by is None
    assert reparsed[0].asked_by is None


def test_empty_open_set_renders_and_parses_as_empty() -> None:
    rendered = render_open_obligations([])
    assert parse_open_obligations(rendered) == ()


def test_a_malformed_row_is_refused_rather_than_silently_dropped() -> None:
    with pytest.raises(ValueError, match="expected 5"):
        parse_open_obligations("| ob-1 | only | three |")


def test_rows_from_records_rejects_an_unknown_state() -> None:
    """A state the schema cannot produce must not render as a blank line."""
    with pytest.raises(ValueError, match="state"):
        rows_from_records(
            [
                {
                    "obligation_id": "ob-1",
                    "state": "probably-fine",
                    "last_event_at": _T,
                }
            ]
        )


def test_delivery_acknowledgements_cite_the_artifact_never_the_ticket() -> None:
    """A14: a ticket id is not a delivery receipt."""
    rendered = render_delivery_acknowledgements(
        [
            _view(
                obligation_id="ob-1",
                state=EnumObligationState.SATISFIED,
                evidence_uri="https://example.invalid/brief.md",
                delivery_state="sent",
                ticket_id="OMN-17019",
            )
        ]
    )
    assert "https://example.invalid/brief.md" in rendered
    assert "sent" in rendered
    assert "OMN-17019" not in rendered


def test_every_closed_state_renders_its_own_closing_evidence() -> None:
    rendered = render_delivery_acknowledgements(
        [
            _view(
                obligation_id="sup-1",
                state=EnumObligationState.SUPERSEDED,
                superseded_by="ob-9",
            ),
            _view(
                obligation_id="aba-1",
                state=EnumObligationState.ABANDONED,
                abandon_reason="operator withdrew the ask",
            ),
        ]
    )
    assert "superseded by ob-9" in rendered
    assert "abandoned: operator withdrew the ask" in rendered


def test_unmet_obligations_for_close_names_the_rows_it_would_block_on() -> None:
    """A6 returns the offending rows, not a bool -- a caller cannot report
    "clean" without naming what it chose to ignore."""
    rows = [
        _view(obligation_id="ob-1"),
        _view(obligation_id="ob-2", state=EnumObligationState.SATISFIED),
    ]
    blocking = unmet_obligations_for_close(rows)
    assert [row.obligation_id for row in blocking] == ["ob-1"]


def test_timestamps_render_in_utc_not_in_the_host_zone() -> None:
    """A naive datetime is treated as already-UTC, never as host-local."""
    naive = datetime(2026, 8, 30, 2, 9, 13)
    rendered = render_open_obligations([_view(last_event_at=naive)])
    assert "2026-08-30T02:09:13Z" in rendered
