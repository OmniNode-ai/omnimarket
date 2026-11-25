# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stand-ins for the publisher and the clock, and the outbox files, of the outbox drain tests."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_ledger_outbox_drain_effect.handlers.handler_outbox_drain import (
    OUTBOX_REFUSED_DIRNAME,
    SendResult,
    outbox_dir,
)

REQ = "11111111-2222-4333-8444-555555555555"
REQ2 = "66666666-7777-4888-8999-000000000000"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOON = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
TOLERANCE_S = 24 * 60 * 60
CLOCK_GUARD_TEXT = (
    "(clock guard: row is stamped 2026-11-09T12:00:00Z but the wall clock is "
    "2026-11-12T12:00:00Z -- 3d BEHIND (tolerance 24h))"
)
FOOTER = "bus-binding footer " * 40 + "END"
_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ")


def stamp(moment: datetime) -> str:
    return moment.strftime(STAMP_FORMAT)


class Clock:
    """A clock that advances ``step_s`` seconds at every read."""

    def __init__(self, start: datetime, step_s: float = 0.0) -> None:
        self.moment = start
        self.step = timedelta(seconds=step_s)

    def __call__(self) -> datetime:
        current = self.moment
        self.moment += self.step
        return current


class Publisher:
    """A sender answering each call with the next of ``answers`` (the last repeats): accepted, duplicate, refused,
    clockguard, bare (a refusal with no message, only stderr), pending."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, payload: str, request_id: str) -> SendResult:
        answer = self.answers[min(len(self.calls), len(self.answers) - 1)]
        self.calls.append((request_id, payload))
        if answer == "pending":
            return SendResult(
                75, request_id, {"status": "pending", "request_id": request_id}, ""
            )
        if answer in {"accepted", "duplicate"}:
            return SendResult(
                0, request_id, {"status": answer, "request_id": request_id}, ""
            )
        message = {
            "refused": "refused by the host",
            "clockguard": CLOCK_GUARD_TEXT,
        }.get(answer, "")
        return SendResult(
            65,
            request_id,
            {"status": "refused", "request_id": request_id, "message": message},
            FOOTER,
        )

    @property
    def ids(self) -> list[str]:
        return [request_id for request_id, _ in self.calls]

    @property
    def rows(self) -> list[str]:
        return [payload for _, payload in self.calls]


class HostGuard:
    """The host's own validation of a request: a row stamped more than the tolerance behind its clock is refused with
    the clock guard's text, and an ``id=`` cell must carry its own row's stamp. Answers accepted otherwise."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []

    def __call__(self, payload: str, request_id: str) -> SendResult:
        self.calls.append((request_id, payload))
        now = self.clock()
        for line in payload.splitlines():
            match = _ROW.match(line)
            if match is None:
                continue
            behind = (
                now - datetime.strptime(match[1], STAMP_FORMAT).replace(tzinfo=UTC)
            ).total_seconds()
            if behind > TOLERANCE_S:
                message = (
                    f"clock guard: row is stamped {match[1]} -- BEHIND (tolerance 24h)"
                )
                return SendResult(65, request_id, {"message": message}, "")
            for cell in line.split(" | "):
                if cell.startswith("id=") and not cell.startswith(f"id={match[1]}-"):
                    return SendResult(
                        65, request_id, {"message": f"id does not carry {match[1]}"}, ""
                    )
        return SendResult(0, request_id, {"status": "accepted"}, "")


def queue(ledger: Path, payload: str, request_id: str, ns: int) -> Path:
    """Keep one stamped request in the outbox the way the writer queues it."""
    directory = outbox_dir(ledger)
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "request_id": request_id,
        "payload": payload,
        "reason": "bus down",
        "queued_at": "2026-11-09T12:00:00Z",
    }
    target = directory / f"{ns:020d}_{request_id}.json"
    target.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    return target


def refused_file(
    ledger: Path, payload: str, request_id: str, ns: int, tried_at: int
) -> Path:
    """A request in refused/, last written (tried) at ``tried_at`` seconds since the epoch."""
    directory = outbox_dir(ledger) / OUTBOX_REFUSED_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "request_id": request_id,
        "payload": payload,
        "reason": "bus down",
        "queued_at": "2026-11-09T12:00:00Z",
        "refusal": "refused by the host",
    }
    target = directory / f"{ns:020d}_{request_id}.json"
    target.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.utime(target, (tried_at, tried_at))
    return target


def stale_row(now: datetime, text: str = "queued while the bus was down") -> str:
    old = stamp(now - timedelta(days=3))
    return f"{old} | STATUS | lane=host-lane | ticket=OMN-19513 | req={REQ} | {text}\n"


def fresh_row(now: datetime, text: str = "a row") -> str:
    return f"{stamp(now)} | STATUS | lane=host-lane | ticket=OMN-19513 | req={REQ} | {text}\n"
