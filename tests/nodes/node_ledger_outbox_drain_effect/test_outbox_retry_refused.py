# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retry of refused outbox files: ``remaining=`` and the file not tried for longest first."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_outbox_drain_effect.__main__ import main
from omnimarket.nodes.node_ledger_outbox_drain_effect.handlers.handler_outbox_drain import (
    OUTBOX_REFUSED_DIRNAME,
    HandlerOutboxDrain,
    outbox_dir,
    outbox_pending,
    outbox_refused,
)
from omnimarket.nodes.node_ledger_outbox_drain_effect.models.model_outbox_drain import (
    ModelOutboxDrainRequest,
)

from .support import NOON, REQ, REQ2, Publisher, fresh_row, queue, refused_file

pytestmark = pytest.mark.unit


def retry(
    ledger: Path, sender: Publisher, *, limit: int = 50
) -> tuple[int, int, int, str]:
    result = HandlerOutboxDrain(sender).handle(
        ModelOutboxDrainRequest(ledger_path=str(ledger), refused=True, limit=limit)
    )
    return result.replayed, result.refused, result.remaining, result.stopped


def test_retry_tries_the_file_not_tried_for_longest_first(ledger: Path) -> None:
    # The names sort REQ first; the file of REQ2 was last tried earlier, so it goes first.
    refused_file(ledger, fresh_row(NOON, "first"), REQ, ns=1, tried_at=2_000_000_000)
    refused_file(ledger, fresh_row(NOON, "second"), REQ2, ns=2, tried_at=1_000_000_000)
    sender = Publisher(["accepted", "accepted"])
    assert retry(ledger, sender, limit=1) == (1, 0, 1, "")
    assert sender.ids == [REQ2]
    assert [r.request_id for r in outbox_refused(ledger)] == [REQ]


def test_a_retried_file_that_is_refused_again_goes_to_the_back(ledger: Path) -> None:
    refused_file(ledger, fresh_row(NOON, "first"), REQ, ns=1, tried_at=1_000_000_000)
    refused_file(ledger, fresh_row(NOON, "second"), REQ2, ns=2, tried_at=1_000_000_100)
    sender = Publisher(["refused"])
    assert retry(ledger, sender, limit=1) == (0, 1, 2, "")
    assert retry(ledger, sender, limit=1) == (0, 1, 2, "")
    assert sender.ids == [REQ, REQ2]


def test_retry_reports_the_refused_files_left_as_remaining(ledger: Path) -> None:
    for ns, rid in enumerate((REQ, REQ2), start=1):
        refused_file(ledger, fresh_row(NOON), rid, ns=ns, tried_at=1_000_000_000 + ns)
    assert retry(ledger, Publisher(["accepted"]), limit=1)[2] == 1
    assert retry(ledger, Publisher(["accepted"]), limit=1)[2] == 0
    assert outbox_refused(ledger) == []


def test_retry_with_nothing_refused_leaves_the_pending_queue_alone(
    ledger: Path,
) -> None:
    queue(ledger, fresh_row(NOON, "still queued"), REQ, 1)
    sender = Publisher(["accepted"])
    assert retry(ledger, sender) == (0, 0, 0, "")
    assert sender.calls == []
    assert len(outbox_pending(ledger)) == 1
    assert (
        HandlerOutboxDrain(sender)
        .handle(ModelOutboxDrainRequest(ledger_path=str(ledger)))
        .replayed
        == 1
    )


def test_retry_stops_on_an_unanswered_bus_and_keeps_the_file(ledger: Path) -> None:
    refused_file(ledger, fresh_row(NOON), REQ, ns=1, tried_at=1_000_000_000)
    replayed, _, remaining, stopped = retry(ledger, Publisher(["pending"]))
    assert (replayed, remaining) == (0, 1)
    assert "exit 75" in stopped


def test_a_stale_tmp_file_a_crashed_drain_left_in_refused_is_removed_by_the_next_drain(
    ledger: Path,
) -> None:
    refused_file(ledger, fresh_row(NOON), REQ, ns=1, tried_at=1_000_000_000)
    leftover = outbox_dir(ledger) / OUTBOX_REFUSED_DIRNAME / ".x.json.tmp"
    leftover.write_text("{", encoding="utf-8")
    assert retry(ledger, Publisher(["accepted"])) == (1, 0, 0, "")
    assert not leftover.exists()


def test_a_drain_that_finds_the_lock_held_returns_at_once(ledger: Path) -> None:
    import fcntl

    queue(ledger, fresh_row(NOON), REQ, 1)
    directory = outbox_dir(ledger)
    with (directory / ".drain.lock").open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sender = Publisher(["accepted"])
        result = HandlerOutboxDrain(sender).handle(
            ModelOutboxDrainRequest(ledger_path=str(ledger))
        )
    assert result.stopped == "another drain holds the outbox"
    assert sender.calls == []
    assert result.remaining == 1


def test_a_drain_with_no_outbox_is_empty(ledger: Path) -> None:
    result = HandlerOutboxDrain(Publisher(["accepted"])).handle(
        ModelOutboxDrainRequest(ledger_path=str(ledger))
    )
    assert (result.replayed, result.refused, result.remaining, result.stopped) == (
        0,
        0,
        0,
        "",
    )


def test_the_drain_verb_reports_and_exits_by_what_is_left(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused_file(ledger, fresh_row(NOON, "a"), REQ, ns=1, tried_at=1_000_000_001)
    refused_file(ledger, fresh_row(NOON, "b"), REQ2, ns=2, tried_at=1_000_000_002)
    base = ["--ledger", str(ledger), "--retry-refused", "--limit", "1"]
    code = main(base, sender=Publisher(["accepted"]))
    assert code == 75
    assert (
        "BUS-RETRY-REFUSED replayed=1 refused=0 remaining=1 stopped=no"
        in capsys.readouterr().out
    )
    assert main(base, sender=Publisher(["refused"])) == 65
    assert "refused=1 remaining=1" in capsys.readouterr().out
    refused_file(ledger, fresh_row(NOON, "a"), REQ, ns=1, tried_at=1_000_000_001)
    assert (
        main(
            ["--ledger", str(ledger), "--retry-refused"], sender=Publisher(["accepted"])
        )
        == 0
    )


def test_the_drain_verb_exits_75_while_requests_wait_for_the_bus(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue(ledger, fresh_row(NOON), REQ, 1)
    assert main(["--ledger", str(ledger)], sender=Publisher(["pending"])) == 75
    assert (
        "BUS-DRAIN replayed=0 refused=0 remaining=1 stopped=request"
        in capsys.readouterr().out
    )


def test_the_verb_without_a_publisher_or_sender_is_indeterminate(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--ledger", str(ledger)]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_the_handler_built_with_no_sender_and_no_publisher_stops_with_the_reason(
    ledger: Path,
) -> None:
    queue(ledger, fresh_row(NOON), REQ, 1)
    result = HandlerOutboxDrain().handle(
        ModelOutboxDrainRequest(ledger_path=str(ledger))
    )
    assert "no publisher" in result.stopped
    assert result.remaining == 1
    body = json.loads(
        next(outbox_dir(ledger).glob("*.json")).read_text(encoding="utf-8")
    )
    assert body["request_id"] == REQ
