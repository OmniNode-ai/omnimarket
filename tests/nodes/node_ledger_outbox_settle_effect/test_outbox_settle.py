# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settling the refused requests of a host's bus outbox: the serve's text is kept, a stale stamp is resent restamped."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_outbox_settle_effect.handlers.handler_outbox_settle import (
    HandlerOutboxSettle,
    SendResult,
    outbox_dir,
    outbox_refused,
    restamp_rows,
)
from omnimarket.nodes.node_ledger_outbox_settle_effect.models.model_outbox_settle import (
    ModelOutboxSettleRequest,
)

pytestmark = pytest.mark.unit

REQ = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NOW = datetime(2026, 11, 15, 8, 30, 0, tzinfo=UTC)
CLASH = "req already belongs to different rows"


def stamp(delta: timedelta = timedelta(0)) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


class Publisher:
    """A stand-in for the serve: answers each send with the next of ``answers`` (the last repeats)."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str]] = []

    def __call__(self, payload: str, request_id: str) -> SendResult:
        self.sent.append((payload, request_id))
        answer = self.answers[min(len(self.sent), len(self.answers)) - 1]
        if answer == "pending":
            return SendResult(75, request_id, {"status": "pending"}, "")
        if answer in {"accepted", "duplicate"}:
            return SendResult(0, request_id, {"status": answer}, "")
        message = (
            answer.removeprefix("refused-")
            if answer != "refused"
            else "refused by the host"
        )
        return SendResult(65, request_id, {"status": "refused", "message": message}, "")


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("", encoding="utf-8")
    return path


def refused_request(ledger: Path, payload: str, request_id: str, refusal: str) -> Path:
    """A refused request in the outbox's ``refused/`` directory, as an earlier replay left it."""
    directory = outbox_dir(ledger) / "refused"
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "request_id": request_id,
        "payload": payload,
        "reason": "bus pending",
        "queued_at": stamp(),
        "refusal": refusal,
    }
    target = directory / f"{len(list(directory.iterdir())):020d}_{request_id}.json"
    target.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    # the file's modification time is the real clock's: put its last refusal two hours behind the injected clock
    refused_at = (NOW - timedelta(hours=2)).timestamp()
    os.utime(target, (refused_at, refused_at))
    return target


def settle(ledger: Path, publisher: Publisher, **fields: object) -> object:
    request = ModelOutboxSettleRequest(ledger_path=str(ledger), now=NOW, **fields)  # type: ignore[arg-type]
    return HandlerOutboxSettle(publisher).handle(request)


def test_settle_removes_a_refused_request_the_serve_now_accepts_and_keeps_a_receipt(
    ledger: Path,
) -> None:
    path = refused_request(
        ledger,
        f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n",
        REQ,
        "refused by the host",
    )
    result = settle(ledger, Publisher(["accepted"]))
    assert (result.settled, result.resent, result.refused, result.remaining) == (
        1,
        0,
        0,
        0,
    )  # type: ignore[attr-defined]
    assert outbox_refused(ledger) == []
    receipt = json.loads((outbox_dir(ledger) / "settled" / path.name).read_text())
    assert receipt["request_id"] == REQ
    assert receipt["how"] == "accepted"
    assert receipt["original"]["refusal"] == "refused by the host"
    assert receipt["settled_at"] == stamp()


def test_settle_resends_a_stale_stamp_restamped_with_the_original_marked(
    ledger: Path,
) -> None:
    old_stamp = stamp(timedelta(days=-3))
    payload = f"{old_stamp} | STATUS | lane=a | req={REQ} | via=bus:h | queued while the bus config was missing\n"
    refused_request(ledger, payload, REQ, "clock guard rejected append -- 24.8h BEHIND")
    publisher = Publisher(
        ["refused-clock guard rejected append -- 24.8h BEHIND", "accepted"]
    )
    result = settle(ledger, publisher)
    assert (result.settled, result.resent, result.refused, result.remaining) == (
        1,
        1,
        0,
        0,
    )  # type: ignore[attr-defined]
    (again, _), (resent, new_id) = publisher.sent
    assert again == payload
    assert new_id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"onex-ledger-settle:{REQ}"))
    cells = resent.rstrip("\n").split(" | ")
    assert cells[0] == stamp()
    assert f"req={new_id}" in cells
    assert f"resent-from={REQ}" in cells
    assert f"original-stamp={old_stamp}" in cells
    assert cells[-1] == "queued while the bus config was missing"
    (receipt,) = (outbox_dir(ledger) / "settled").glob("*.json")
    assert json.loads(receipt.read_text())["how"] == "resent-restamped"


def test_settle_leaves_a_real_clash_and_a_fresh_stamp_refusal_with_the_serves_text(
    ledger: Path,
) -> None:
    fresh, clash = REQ, "ffffffff-1111-4222-8333-444444444444"
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={fresh} | a fresh row\n", fresh, "x"
    )
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={clash} | a clash\n", clash, "x"
    )
    result = settle(
        ledger,
        Publisher(["refused-the serve refuses the row itself", f"refused-{CLASH}"]),
    )
    assert (result.settled, result.resent, result.refused, result.remaining) == (
        0,
        0,
        2,
        2,
    )  # type: ignore[attr-defined]
    left = outbox_refused(ledger)
    assert [r.request_id for r in left] == [fresh, clash]
    assert left[0].refusal == "the serve refuses the row itself"
    assert left[1].refusal == CLASH


def test_settle_stops_when_the_bus_cannot_answer_and_leaves_the_request(
    ledger: Path,
) -> None:
    refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    result = settle(ledger, Publisher(["pending"]))
    assert (result.settled, result.remaining) == (0, 1)  # type: ignore[attr-defined]
    assert "exit 75" in result.stopped  # type: ignore[attr-defined]
    assert [r.request_id for r in outbox_refused(ledger)] == [REQ]


def test_a_refusal_younger_than_the_retry_interval_is_left_alone_until_it_has_waited(
    ledger: Path,
) -> None:
    path = refused_request(
        ledger, f"{stamp()} | STATUS | lane=a | req={REQ} | a row\n", REQ, "refused"
    )
    refused_at = (NOW - timedelta(seconds=60)).timestamp()
    os.utime(path, (refused_at, refused_at))
    publisher = Publisher(["accepted"])
    waiting = settle(ledger, publisher, min_age_s=3600)
    assert (waiting.settled, waiting.remaining) == (0, 1)  # type: ignore[attr-defined]
    assert publisher.sent == []
    assert settle(ledger, publisher, operation="due").due is False  # type: ignore[attr-defined]

    refused_at = (NOW - timedelta(seconds=3601)).timestamp()
    os.utime(path, (refused_at, refused_at))
    assert settle(ledger, publisher, operation="due").due is True  # type: ignore[attr-defined]
    done = settle(ledger, publisher, min_age_s=3600)
    assert (done.settled, done.remaining) == (1, 0)  # type: ignore[attr-defined]
    assert len(publisher.sent) == 1


def test_restamp_rows_marks_the_original_and_changes_nothing_else() -> None:
    payload = (
        f"2026-11-12T07:00:00Z | STATUS | lane=a | req={REQ} | via=bus:h | the row's text\n"
        "  a continuation line the row carries on the next line\n"
    )
    new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"onex-ledger-settle:{REQ}"))
    assert restamp_rows(payload, REQ, NOW) == (
        f"2026-11-15T08:30:00Z | STATUS | lane=a | req={new_id} | via=bus:h | "
        f"resent-from={REQ} | original-stamp=2026-11-12T07:00:00Z | the row's text\n"
        "  a continuation line the row carries on the next line\n"
    )


def test_nothing_refused_is_a_clean_zero(ledger: Path) -> None:
    result = settle(ledger, Publisher(["accepted"]))
    assert (result.settled, result.refused, result.remaining, result.stopped) == (
        0,
        0,
        0,
        "",
    )  # type: ignore[attr-defined]
