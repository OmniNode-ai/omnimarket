# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the queue lifecycle record and terminal-reason taxonomy [OMN-17018].

Covers the two halves of the ticket that are pure type/behaviour contracts:
the append-only lifecycle record with a renewable lease, and the terminal-reason
enum whose recovery policy is encoded on the member so ``unknown`` is
non-redispatchable *by construction* rather than by a caller remembering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.enums.enum_dispatch_queue_phase import EnumDispatchQueuePhase
from omnimarket.enums.enum_dispatch_terminal_reason import (
    EnumDispatchTerminalDisposition,
    EnumDispatchTerminalReason,
)
from omnimarket.nodes.node_dispatch_queue_drainer.handlers.dispatch_queue_lifecycle_ledger import (
    FileDispatchQueueLifecycleLedger,
    InvalidLifecycleTransitionError,
    ProtocolDispatchQueueLifecycleLedger,
    blocked_terminal,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueTerminal,
)

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

#: Every reason the ticket enumerates, with the recovery verdict it must carry.
#: ``deliberate_cancellation`` and ``unknown`` are refused; the rest describe
#: environmental or transient stops where the work itself was never rejected.
_REASON_POLICY: tuple[tuple[EnumDispatchTerminalReason, bool], ...] = (
    (EnumDispatchTerminalReason.DELIBERATE_CANCELLATION, False),
    (EnumDispatchTerminalReason.USER_STOP, True),
    (EnumDispatchTerminalReason.SESSION_QUOTA, True),
    (EnumDispatchTerminalReason.PROCESS_LOSS, True),
    (EnumDispatchTerminalReason.DEPENDENCY_FAILURE, True),
    (EnumDispatchTerminalReason.HOST_OVERLOAD, True),
    (EnumDispatchTerminalReason.TIMEOUT, True),
    (EnumDispatchTerminalReason.UNKNOWN, False),
)


@pytest.mark.unit
def test_taxonomy_members_are_exactly_the_declared_eight() -> None:
    """The enum is the ticket's list verbatim — no member added or dropped."""
    assert {reason.value for reason in EnumDispatchTerminalReason} == {
        "deliberate_cancellation",
        "user_stop",
        "session_quota",
        "process_loss",
        "dependency_failure",
        "host_overload",
        "timeout",
        "unknown",
    }


@pytest.mark.unit
@pytest.mark.parametrize(("reason", "redispatchable"), _REASON_POLICY)
def test_recovery_policy_is_encoded_on_the_reason(
    reason: EnumDispatchTerminalReason, redispatchable: bool
) -> None:
    """DoD 6: policy keys off the reason, and is carried by the reason itself."""
    assert reason.auto_redispatchable is redispatchable


@pytest.mark.unit
def test_unknown_is_non_redispatchable_by_construction() -> None:
    """An unclassifiable stop must never default to retry, or to healthy."""
    assert EnumDispatchTerminalReason.UNKNOWN.auto_redispatchable is False
    terminal = blocked_terminal(EnumDispatchTerminalReason.UNKNOWN)
    assert terminal.auto_redispatchable is False


@pytest.mark.unit
def test_stopped_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="must carry a terminal reason"):
        ModelDispatchQueueTerminal(
            disposition=EnumDispatchTerminalDisposition.STOPPED, reason=None
        )


@pytest.mark.unit
def test_completed_must_not_carry_a_stop_reason() -> None:
    """A completed lane is not a stop; a nullable reason must not smuggle one in."""
    with pytest.raises(ValueError, match="must not carry a stop reason"):
        ModelDispatchQueueTerminal(
            disposition=EnumDispatchTerminalDisposition.COMPLETED,
            reason=EnumDispatchTerminalReason.TIMEOUT,
        )


@pytest.mark.unit
def test_completed_lane_is_not_a_redispatch_candidate() -> None:
    terminal = ModelDispatchQueueTerminal(
        disposition=EnumDispatchTerminalDisposition.COMPLETED
    )
    assert terminal.reason is None
    assert terminal.auto_redispatchable is False


def _ledger(tmp_path: Path) -> FileDispatchQueueLifecycleLedger:
    return FileDispatchQueueLifecycleLedger(tmp_path / "lifecycle")


def _item(tmp_path: Path, name: str = "item.yaml") -> Path:
    path = tmp_path / name
    path.write_text("placeholder\n", encoding="utf-8")
    return path


@pytest.mark.unit
def test_file_ledger_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(_ledger(tmp_path), ProtocolDispatchQueueLifecycleLedger)


@pytest.mark.unit
def test_lifecycle_is_append_only_across_the_full_phase_chain(tmp_path: Path) -> None:
    """QUEUED -> CLAIMED -> DISPATCHED -> STARTED -> TERMINAL, all durable."""
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)

    assert ledger.load(item) is None  # QUEUED == no record yet

    ledger.claim(item, actor="drainer", lease_seconds=60, now=_T0)
    ledger.mark_dispatched(
        item, actor="drainer", ack_timeout_seconds=60, now=_T0, detail="handed off"
    )
    ledger.acknowledge_started(
        item, actor="lane-1", now=_T0 + timedelta(seconds=5), detail="lane started"
    )
    final = ledger.mark_terminal(
        item,
        actor="lane-1",
        terminal=ModelDispatchQueueTerminal(
            disposition=EnumDispatchTerminalDisposition.COMPLETED
        ),
        now=_T0 + timedelta(seconds=10),
        detail="work finished",
    )

    assert [transition.phase for transition in final.transitions] == [
        EnumDispatchQueuePhase.CLAIMED,
        EnumDispatchQueuePhase.DISPATCHED,
        EnumDispatchQueuePhase.STARTED,
        EnumDispatchQueuePhase.TERMINAL,
    ]
    assert final.phase is EnumDispatchQueuePhase.TERMINAL
    assert item.exists(), "the lifecycle must never delete the queue item"
    # the record round-trips from disk with its full history intact
    reloaded = ledger.load(item)
    assert reloaded is not None
    assert reloaded.transitions == final.transitions


@pytest.mark.unit
def test_lease_is_renewable_and_expiry_marks_stale_without_deleting(
    tmp_path: Path,
) -> None:
    """Operator ruling A1-REVISED: renewable lease, expiry marks stale, never deletes."""
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)
    ledger.claim(item, actor="drainer", lease_seconds=60, now=_T0)

    after_expiry = _T0 + timedelta(seconds=61)
    expired = ledger.load(item)
    assert expired is not None
    assert expired.is_stale(after_expiry) is True
    assert expired.phase is EnumDispatchQueuePhase.CLAIMED, (
        "a stale claim keeps its phase — it is not silently reset to QUEUED"
    )
    assert item.exists()

    renewed = ledger.renew(
        item, actor="drainer", lease_seconds=600, now=_T0 + timedelta(seconds=30)
    )
    assert renewed.is_stale(after_expiry) is False
    assert renewed.phase is EnumDispatchQueuePhase.CLAIMED
    assert len(renewed.transitions) == 2, "renewal is appended, not rewritten"


@pytest.mark.unit
def test_dispatched_item_is_pending_until_acknowledged(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)
    ledger.claim(item, actor="drainer", lease_seconds=60, now=_T0)
    dispatched = ledger.mark_dispatched(
        item, actor="drainer", ack_timeout_seconds=30, now=_T0, detail="handed off"
    )

    assert dispatched.is_pending_acknowledgement(_T0) is True
    assert dispatched.acknowledgement_timed_out(_T0) is False
    assert dispatched.acknowledgement_timed_out(_T0 + timedelta(seconds=31)) is True

    started = ledger.acknowledge_started(
        item, actor="lane-1", now=_T0 + timedelta(seconds=5), detail="ack"
    )
    assert started.is_pending_acknowledgement(_T0 + timedelta(seconds=31)) is False
    assert started.acknowledgement_timed_out(_T0 + timedelta(seconds=31)) is False


@pytest.mark.unit
def test_claiming_an_already_claimed_item_is_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)
    ledger.claim(item, actor="drainer-a", lease_seconds=60, now=_T0)
    with pytest.raises(InvalidLifecycleTransitionError, match="already at phase"):
        ledger.claim(item, actor="drainer-b", lease_seconds=60, now=_T0)


@pytest.mark.unit
def test_out_of_order_transitions_fail_loudly(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)

    with pytest.raises(InvalidLifecycleTransitionError, match="no lifecycle record"):
        ledger.mark_dispatched(
            item, actor="drainer", ack_timeout_seconds=60, now=_T0, detail="x"
        )

    ledger.claim(item, actor="drainer", lease_seconds=60, now=_T0)
    with pytest.raises(InvalidLifecycleTransitionError, match="acknowledge requires"):
        ledger.acknowledge_started(item, actor="lane", now=_T0, detail="x")


@pytest.mark.unit
def test_terminal_is_written_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)
    terminal = blocked_terminal(EnumDispatchTerminalReason.HOST_OVERLOAD)
    ledger.mark_terminal(item, actor="w", terminal=terminal, now=_T0, detail="shed")
    with pytest.raises(InvalidLifecycleTransitionError, match="already TERMINAL"):
        ledger.mark_terminal(item, actor="w", terminal=terminal, now=_T0, detail="shed")


@pytest.mark.unit
def test_zero_length_lease_is_rejected(tmp_path: Path) -> None:
    """No defensive default window — a non-positive lease fails fast."""
    ledger = _ledger(tmp_path)
    item = _item(tmp_path)
    with pytest.raises(ValueError, match="positive number of seconds"):
        ledger.claim(item, actor="drainer", lease_seconds=0, now=_T0)
