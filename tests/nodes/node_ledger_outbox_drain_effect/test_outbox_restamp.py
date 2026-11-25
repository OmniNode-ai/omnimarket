# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The restamp of the outbox drain: only a clock-guard refusal restamps, the citing cells follow, one request id throughout.

The cases of the retired ledger bus writer's host-on-bus tests (stale row, cited stamps, same request id, advancing
clock, mirror, host-validation text), run against the node's handler.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_outbox_drain_effect.handlers.handler_outbox_drain import (
    OUTBOX_RESTAMP_AFTER_S,
    HandlerOutboxDrain,
    SendResult,
    outbox_pending,
    outbox_refused,
    refusal_text,
    restamp_stale,
)
from omnimarket.nodes.node_ledger_outbox_drain_effect.models.model_outbox_drain import (
    ModelOutboxDrainRequest,
)

from .support import (
    NOON,
    REQ,
    TOLERANCE_S,
    Clock,
    HostGuard,
    Publisher,
    fresh_row,
    queue,
    stale_row,
    stamp,
)

pytestmark = pytest.mark.unit


def drain(
    ledger: Path, sender: Publisher | HostGuard, clock: Clock, *, refused: bool = False
) -> tuple[int, int, int, str]:
    handler = HandlerOutboxDrain(sender, clock)
    result = handler.handle(
        ModelOutboxDrainRequest(ledger_path=str(ledger), refused=refused)
    )
    return result.replayed, result.refused, result.remaining, result.stopped


def test_restamp_threshold_sits_inside_the_clock_guard_tolerance() -> None:
    assert OUTBOX_RESTAMP_AFTER_S < TOLERANCE_S
    old = f"{stamp(NOON - timedelta(seconds=OUTBOX_RESTAMP_AFTER_S + 1))} | STATUS | lane=a | x\n"
    assert restamp_stale(old, NOON).startswith(stamp(NOON))
    edge = f"{stamp(NOON - timedelta(seconds=OUTBOX_RESTAMP_AFTER_S))} | STATUS | lane=a | x\n"
    assert restamp_stale(edge, NOON) == edge


def test_a_stale_queued_request_is_sent_stored_first_and_restamped_after_the_clock_guard_refuses(
    ledger: Path,
) -> None:
    sender = Publisher(["clockguard", "accepted"])
    queued_row = stale_row(NOON)
    queue(ledger, queued_row, REQ, 1)
    assert drain(ledger, sender, Clock(NOON)) == (1, 0, 0, "")
    first, second = sender.rows
    assert first == queued_row
    assert second.startswith(f"{stamp(NOON)} | STATUS | ")
    assert second.count("late-replay-of=") == 1
    old = queued_row.split(" | ")[0]
    assert f" | late-replay-of={old} | queued while" in second
    assert sender.ids == [REQ, REQ]
    assert outbox_pending(ledger) == []
    assert outbox_refused(ledger) == []


def test_a_refusal_that_is_not_the_clock_guard_never_restamps(ledger: Path) -> None:
    sender = Publisher(["refused", "accepted"])
    queued_row = stale_row(NOON)
    queue(ledger, queued_row, REQ, 1)
    assert drain(ledger, sender, Clock(NOON)) == (0, 1, 0, "")
    (refused,) = outbox_refused(ledger)
    assert refused.payload == queued_row
    assert sender.rows == [queued_row]
    assert refused.refusal == "refused by the host"


def test_a_request_answered_pending_and_drained_after_the_bound_goes_out_as_stored(
    ledger: Path,
) -> None:
    """Sent fresh, the receipt lost (the host holds the rows), drained 25h later: the host answers the stored bytes
    duplicate before its clock guard, so nothing may be restamped."""
    sender = Publisher(["duplicate"])
    queued_row = fresh_row(NOON)
    queue(ledger, queued_row, REQ, 1)
    later = Clock(NOON + timedelta(hours=25), step_s=11)
    assert drain(ledger, sender, later) == (1, 0, 0, "")
    assert sender.rows == [queued_row]
    assert "late-replay-of" not in sender.rows[0]
    assert outbox_pending(ledger) == []
    assert outbox_refused(ledger) == []


def test_a_restamped_request_that_waits_a_second_time_is_sent_stored_then_restamped_again(
    ledger: Path,
) -> None:
    sender = Publisher(["clockguard", "pending", "clockguard", "accepted"])
    old = stale_row(NOON).split(" | ")[0]
    queue(ledger, stale_row(NOON), REQ, 1)
    # First drain: the clock guard refuses, the restamped bytes are kept, then the bus does not answer.
    replayed, _, remaining, stopped = drain(ledger, sender, Clock(NOON))
    assert (replayed, remaining) == (0, 1)
    assert "exit 75" in stopped
    (queued,) = outbox_pending(ledger)
    assert queued.payload.startswith(stamp(NOON))
    later = NOON + timedelta(hours=25)
    assert drain(ledger, sender, Clock(later, step_s=5)) == (1, 0, 0, "")
    rows = sender.rows
    assert rows[0].startswith(old)
    assert rows[1] == rows[2] == queued.payload
    assert rows[3].startswith(stamp(later))
    assert rows[3].count("late-replay-of=") == 1
    assert f"late-replay-of={old}" in rows[3]


def test_a_stale_request_keeps_one_request_id_across_every_replay_with_a_stepping_clock(
    ledger: Path,
) -> None:
    sender = Publisher(["clockguard", "pending", "accepted"])
    queue(ledger, stale_row(NOON), REQ, 1)
    clock = Clock(NOON, step_s=3 * 60 * 60)
    assert drain(ledger, sender, clock)[3] != ""
    assert drain(ledger, sender, clock)[0] == 1
    assert len(sender.calls) == 3
    assert sender.ids == [REQ, REQ, REQ]
    assert len(set(sender.rows)) == 2


def test_the_restamped_row_passes_host_validation_and_the_stale_one_hits_the_clock_guard(
    ledger: Path,
) -> None:
    clock = Clock(NOON)
    host = HostGuard(clock)
    stale = stale_row(NOON)
    queue(ledger, stale, REQ, 1)
    assert drain(ledger, host, clock) == (1, 0, 0, "")
    assert len(host.calls) == 2
    first = host(stale, REQ)
    assert first.exit_code == 65
    assert "clock guard" in refusal_text(first)
    assert "BEHIND" in refusal_text(first)
    assert host(host.calls[1][1], REQ).exit_code == 0
    assert host.calls[0][1] == stale
    assert host.calls[1][1] != stale


def test_restamp_of_a_claim_and_its_terminal_across_the_bound_keeps_the_citation() -> (
    None
):
    claim_stamp, msg_stamp = "2026-11-09T12:00:00Z", "2026-11-09T12:05:00Z"
    claim = f"{claim_stamp} | CLAIM | lane=a | ticket=OMN-19513 | host=h | work"
    terminal = (
        f"2026-11-09T13:00:00Z | TERMINAL | lane=a | ticket=OMN-19513 | closes-CLAIM={claim_stamp}"
        " | outcome=done | friction=none | done"
    )
    closes = (
        f"2026-11-09T13:05:00Z | RELEASE | lane=a | closes={claim_stamp} | released"
    )
    msg = f"{msg_stamp} | MSG | from=a | to=b | id={msg_stamp}-a | text"
    ack = (
        f"{msg_stamp} | ACK | from=b | to=a | id={msg_stamp}-b | re={msg_stamp}-a | ok"
    )
    fresh = "2026-11-12T11:30:00Z | STATUS | lane=a | fresh and unrelated"
    out = restamp_stale(
        "\n".join([claim, terminal, closes, msg, ack, fresh]) + "\n", NOON
    ).splitlines()
    now_stamp = stamp(NOON)
    assert out[0].startswith(f"{now_stamp} | CLAIM | ")
    assert f"closes-CLAIM={now_stamp} | outcome=done" in out[1]
    assert (
        f"| closes={now_stamp} | late-replay-of=2026-11-09T13:05:00Z | released"
        in out[2]
    )
    assert f"id={now_stamp}-a" in out[3]
    assert "id=" + msg_stamp not in out[3]
    assert f"id={now_stamp}-b | re={now_stamp}-a" in out[4]
    assert out[5] == fresh
    # The old stamps survive only in the late-replay-of cells.
    cells = [cell for line in out for cell in line.split(" | ")]
    assert all(
        claim_stamp not in cell
        for cell in cells
        if not cell.startswith("late-replay-of=")
    )


def test_a_restamp_in_a_later_request_follows_a_claim_restamped_earlier_in_the_drain() -> (
    None
):
    stamps: dict[str, str] = {}
    claim = "2026-11-09T12:00:00Z | CLAIM | lane=a | ticket=OMN-19513 | work\n"
    terminal = "2026-11-09T13:00:00Z | TERMINAL | lane=a | closes-CLAIM=2026-11-09T12:00:00Z | done\n"
    restamp_stale(claim, NOON, stamps)
    assert "closes-CLAIM=2026-11-12T12:00:00Z" in restamp_stale(terminal, NOON, stamps)


def test_a_late_replay_of_in_the_free_text_does_not_stop_the_cell_being_added() -> None:
    line = (
        "2026-11-09T12:00:00Z | STATUS | lane=a | see | late-replay-of=x in the text\n"
    )
    assert "late-replay-of=2026-11-09T12:00:00Z" in restamp_stale(line, NOON)


def test_a_row_already_carrying_its_late_replay_cell_keeps_that_cell_alone() -> None:
    line = "2026-11-09T12:00:00Z | STATUS | lane=a | late-replay-of=2026-11-01T00:00:00Z | text\n"
    out = restamp_stale(line, NOON)
    assert out.count("late-replay-of=") == 1
    assert "late-replay-of=2026-11-01T00:00:00Z" in out


def test_a_long_refusal_message_keeps_its_head_and_the_stderr_fallback_keeps_its_tail() -> (
    None
):
    long = "the cause first " + "x" * 900
    from_message = SendResult(65, REQ, {"message": long}, "ignored")
    assert refusal_text(from_message) == long[:600]
    from_stderr = SendResult(65, REQ, None, "head " + "y" * 900 + " tail")
    assert refusal_text(from_stderr).endswith("tail")
    assert len(refusal_text(from_stderr)) == 600


def test_the_drain_leaves_no_original_stamp_copy_in_the_ledger_file(
    ledger: Path,
) -> None:
    """The drain never writes the ledger file: the host appends the rows it received, so a file as left holds no row
    under the original stamp, which the host never got under that stamp (the bus check names such a copy)."""
    before = ledger.read_bytes()
    sender = Publisher(["pending", "clockguard", "accepted"])
    queued_row = fresh_row(NOON)
    queue(ledger, queued_row, REQ, 1)
    assert drain(ledger, sender, Clock(NOON + timedelta(hours=25)))[3] != ""
    assert drain(ledger, sender, Clock(NOON + timedelta(hours=25))) == (1, 0, 0, "")
    first, _, restamped = sender.rows
    assert first == queued_row
    assert restamped != first
    assert ledger.read_bytes() == before
    assert queued_row.split(" | ")[0] not in ledger.read_text(encoding="utf-8")


def test_the_request_file_keeps_the_restamped_bytes_when_the_resend_is_refused(
    ledger: Path,
) -> None:
    sender = Publisher(["clockguard", "refused"])
    queue(ledger, stale_row(NOON), REQ, 1)
    assert drain(ledger, sender, Clock(NOON)) == (0, 1, 0, "")
    (refused,) = outbox_refused(ledger)
    assert refused.payload == sender.rows[1]
    assert refused.payload.startswith(stamp(NOON))
    body = json.loads(refused.path.read_text(encoding="utf-8"))
    assert body["request_id"] == REQ
    assert body["refusal"] == "refused by the host"
