# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane that ended in a held terminal outcome is not resent while the ticket is unchanged (OMN-19998).

2026-11-14, 00:10Z to 00:30Z: about 35 lab-fill lanes ended with nothing to do and the fill sent the
same ticket again. Each lane's newest TERMINAL was skipped-already-built (falsifiers pass or fail),
done, or blocked on a start criterion, and no gate held the ticket: the selection only knew the
outcomes already-built, skipped-owned and skipped, and only with a merged PR known, so
skipped-already-built was never a reason to wait. The deployment now names the terminal outcomes to
hold and the window they hold for; a ticket that changed since the lane ended, or a window that
elapsed, is released.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionOverlay,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-14T00:40:00Z"
LATE = "2026-11-15T00:40:00Z"
HOLD_DEPLOYMENT = ModelLabFillDeployment(
    ("change_control",),
    ("registry",),
    ("docs",),
    terminal_hold_outcomes=(
        "skipped-already-built",
        "already-built",
        "done",
        "blocked",
    ),
    terminal_hold_window_hours=24,
)
OFF_DEPLOYMENT = ModelLabFillDeployment(("change_control",), ("registry",), ("docs",))
# The ledger rows of the 2026-11-14 lanes, as recorded (host and request cells dropped). Each lane
# has a CLAIM row before its TERMINAL so the replay counts the attempt.
TODAY = (
    "2026-11-14T00:10:00Z | CLAIM | lane=lab-fill-omn23021-11132350 | ticket=OMN-23021 | priority=other",
    "2026-11-14T00:12:00Z | TERMINAL | lane=lab-fill-omn23021-11132350 | ticket=OMN-23021 | outcome=skipped-already-built | falsifiers=pass",
    "2026-11-14T00:11:00Z | CLAIM | lane=lab-fill-omn22112-11132350 | ticket=OMN-22112 | priority=other",
    "2026-11-14T00:13:00Z | TERMINAL | lane=lab-fill-omn22112-11132350 | ticket=OMN-22112 | outcome=skipped-already-built | falsifiers=fail",
    "2026-11-14T00:12:00Z | CLAIM | lane=lab-fill-omn22972-11132350 | ticket=OMN-22972 | priority=other",
    "2026-11-14T00:14:00Z | TERMINAL | lane=lab-fill-omn22972-11132350 | ticket=OMN-22972 | outcome=blocked",
    "2026-11-14T00:13:00Z | CLAIM | lane=lab-fill-omn22964-11132350 | ticket=OMN-22964 | priority=other",
    "2026-11-14T00:15:00Z | TERMINAL | lane=lab-fill-omn22964-11132350 | ticket=OMN-22964 | outcome=blocked",
    "2026-11-14T00:14:00Z | CLAIM | lane=lab-fill-omn23053-11132350 | ticket=OMN-23053 | priority=other",
    "2026-11-14T00:16:00Z | TERMINAL | lane=lab-fill-omn23053-11132350 | ticket=OMN-23053 | outcome=done",
)


def _candidate(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": f"ticket:{ticket}",
        "kind": "ticket",
        "ticket": ticket,
        "ticket_updated_at": "2026-11-13T20:00:00Z",
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _select(
    candidates: tuple[ModelLabFillCandidateInput, ...],
    ledger: tuple[str, ...] = TODAY,
    deployment: ModelLabFillDeployment = HOLD_DEPLOYMENT,
    now: str = NOW,
) -> ModelLabFillSelectionResult:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=now,
            candidates=candidates,
            ledger_lines=ledger,
            deployment=deployment,
            idle_slots=4,
        )
    )


def test_skipped_already_built_with_falsifiers_pass_is_already_built_not_dispatched() -> (
    None
):
    result = _select((_candidate("OMN-23021"),))

    assert result.decisions[0].reason is EnumLabFillSkipReason.ALREADY_BUILT
    assert (
        result.decisions[0].caller_reason
        == "already-built:falsifiers-pass:lab-fill-omn23021-11132350"
    )
    assert result.decisions[0].detail == "lab-fill-omn23021-11132350"
    assert [d.ticket for d in result.closer] == ["OMN-23021"]
    assert result.readback == ()


def test_skipped_already_built_with_falsifiers_fail_is_readback() -> None:
    result = _select((_candidate("OMN-22112"),))

    assert result.decisions[0].reason is EnumLabFillSkipReason.READBACK
    assert result.decisions[0].caller_reason == "readback:falsifiers-fail"
    assert result.decisions[0].detail == "lab-fill-omn22112-11132350"
    assert [d.ticket for d in result.readback] == ["OMN-22112"]


def test_blocked_and_done_terminals_are_terminal_held() -> None:
    result = _select(
        (
            _candidate("OMN-22972"),
            _candidate("OMN-22964"),
            _candidate("OMN-23053"),
        )
    )

    by_ticket = {d.ticket: d for d in result.decisions}
    for ticket in ("OMN-22972", "OMN-22964", "OMN-23053"):
        assert by_ticket[ticket].reason is EnumLabFillSkipReason.TERMINAL_HELD
    assert by_ticket["OMN-22972"].caller_reason == "terminal-held:blocked"
    assert by_ticket["OMN-22964"].caller_reason == "terminal-held:blocked"
    assert by_ticket["OMN-23053"].caller_reason == "terminal-held:done"
    assert result.closer == ()
    assert result.readback == ()


def test_hold_off_deployment_leaves_the_tickets_dispatchable() -> None:
    result = _select(
        (
            _candidate("OMN-23021"),
            _candidate("OMN-22112"),
            _candidate("OMN-23053"),
        ),
        deployment=OFF_DEPLOYMENT,
    )

    by_ticket = {d.ticket: d for d in result.decisions}
    for ticket in ("OMN-23021", "OMN-22112", "OMN-23053"):
        assert by_ticket[ticket].reason is None
    assert result.closer == ()
    assert result.readback == ()


def test_a_ticket_updated_after_the_terminal_is_released() -> None:
    result = _select(
        (_candidate("OMN-23021", ticket_updated_at="2026-11-14T00:20:00Z"),),
    )

    assert result.decisions[0].reason is None


def test_a_criterion_changed_after_the_terminal_is_released() -> None:
    result = _select(
        (_candidate("OMN-23021", criterion_changed_at="2026-11-14T00:20:00Z"),),
    )

    assert result.decisions[0].reason is None


def test_an_elapsed_window_releases_the_hold() -> None:
    result = _select((_candidate("OMN-23021"),), now=LATE)

    assert result.decisions[0].reason is None


def test_a_negative_window_or_a_non_list_of_outcomes_is_rejected() -> None:
    with pytest.raises(ValueError, match="terminal_hold_window_hours"):
        ModelLabFillDeployment.from_overlay(
            {
                "companion_repositories": ["change_control"],
                "not_work_repositories": ["registry"],
                "document_repositories": ["docs"],
                "terminal_hold_window_hours": -1,
            }
        )
    with pytest.raises(ValueError, match="terminal_hold_outcomes"):
        ModelLabFillDeployment.from_overlay(
            {
                "companion_repositories": ["change_control"],
                "not_work_repositories": ["registry"],
                "document_repositories": ["docs"],
                "terminal_hold_outcomes": "skipped-already-built",
            }
        )


def test_the_overlay_carries_the_two_keys_through_and_defaults_hold_off() -> None:
    overlay = ModelLabFillSelectionOverlay(
        companion_repositories=["change_control"],
        not_work_repositories=["registry"],
        document_repositories=["docs"],
        terminal_hold_outcomes=["skipped-already-built", "done"],
        terminal_hold_window_hours=24,
    )
    deployment = overlay.deployment()
    assert deployment.terminal_hold_outcomes == ("skipped-already-built", "done")
    assert deployment.terminal_hold_window_hours == 24

    bare = ModelLabFillSelectionOverlay(
        companion_repositories=["change_control"],
        not_work_repositories=["registry"],
        document_repositories=["docs"],
    )
    assert bare.deployment().terminal_hold_outcomes == ()
    assert bare.deployment().terminal_hold_window_hours == 0


def test_the_golden_chain_of_the_five_tickets_plus_a_never_attempted_one() -> None:
    result = _select(
        (
            _candidate("OMN-23021"),
            _candidate("OMN-22112"),
            _candidate("OMN-22972"),
            _candidate("OMN-22964"),
            _candidate("OMN-23053"),
            _candidate("OMN-1"),
        )
    )

    expected = (
        (
            "OMN-23021",
            EnumLabFillSkipReason.ALREADY_BUILT,
            "already-built:falsifiers-pass:lab-fill-omn23021-11132350",
        ),
        ("OMN-22112", EnumLabFillSkipReason.READBACK, "readback:falsifiers-fail"),
        ("OMN-22972", EnumLabFillSkipReason.TERMINAL_HELD, "terminal-held:blocked"),
        ("OMN-22964", EnumLabFillSkipReason.TERMINAL_HELD, "terminal-held:blocked"),
        ("OMN-23053", EnumLabFillSkipReason.TERMINAL_HELD, "terminal-held:done"),
        ("OMN-1", None, ""),
    )
    actual = tuple((d.ticket, d.reason, d.caller_reason) for d in result.decisions)
    assert actual == expected
