# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable local outbox for ledger appends made while the bus binding is degraded (OMN-20275).

An append on the in-memory binding has no ledger host to reach. It is written here instead: one JSON file
per request, written to a temporary name, fsynced and renamed, so a reader never sees half a request.
The request id is the idempotency key the ledger host's serve deduplicates on, so a replay of a request
whose first attempt did land is answered ``duplicate`` and the row is appended once. Accepted or
duplicate removes the file; a refusal is final and moves it to ``refused/``, where the replay's caller
reports it to the writer's lane (recovery.report_refused_to_writers); any other answer stops the drain and
leaves the file for the next one. One drain at a time (an fcntl lock).

A request id the ledger host refused as a clash (the id already belongs to other rows) is refused again on every
replay with the same words, so a replay of the same rows under that id is answered from the stored refusal and
never sent: it adds no second file to ``refused/`` and no second report for the writer (OMN-24250). A refusal
for any other cause, and corrected rows under the same id, are still sent, because their cause can have gone.

A retry of a row whose first attempt got no answer (queued, or timed out) reuses that attempt's request id
and rows, so the serve answers ``duplicate`` instead of appending or acknowledging the row a second time.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from omnimarket.models.bus_binding import ModelBusBindingDecision
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

OUTBOX_DIRNAME = "work-ledger-outbox"
REFUSED_DIRNAME = "refused"
DRAIN_LOCK_NAME = ".drain.lock"
ATTEMPTS_DIRNAME = "attempts"
# What the ledger host's append handler writes when a request id already belongs to other rows or another
# principal (node_work_ledger_append_effect); a seam test builds the real refusal and fails if this drifts.
CLASH_MARK = "already belongs to different rows or principal"
# A retry follows its first attempt within minutes; past this the same words are a new row.
RETRY_WINDOW = timedelta(minutes=30)
_REQ_CELL = re.compile(r" \| req=[0-9a-fA-F-]{36}(?= \||$)")
_ROW_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.MULTILINE)

Sender = Callable[
    [ModelWorkLedgerAppendRequest], Awaitable[ModelWorkLedgerAppendReceipt]
]


@dataclass(frozen=True)
class DrainResult:
    replayed: int
    duplicates: int
    refused: int
    remaining: int
    stopped: str | None


def outbox_dir(state_dir: Path) -> Path:
    return state_dir / OUTBOX_DIRNAME


def queue_request(state_dir: Path, request: ModelWorkLedgerAppendRequest) -> Path:
    """Keep one request for replay. Idempotent on the request id. Raises OSError when it cannot be written."""
    directory = outbox_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for existing in directory.glob(f"*_{request.request_id}.json"):
        return existing
    name = f"{time.time_ns():020d}_{request.request_id}.json"
    target = directory / name
    tmp = directory / f".{name}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(request.model_dump_json())
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(target)
    return target


def pending(state_dir: Path) -> list[tuple[Path, ModelWorkLedgerAppendRequest]]:
    """Queued requests, oldest first. An unreadable file is left out and left alone."""
    directory = outbox_dir(state_dir)
    found: list[tuple[Path, ModelWorkLedgerAppendRequest]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(
                (
                    path,
                    ModelWorkLedgerAppendRequest.model_validate_json(
                        path.read_text("utf-8")
                    ),
                )
            )
        except (OSError, ValueError):
            continue
    return found


def pending_count(state_dir: Path) -> int:
    return len(pending(state_dir))


def stored_refusal(
    state_dir: Path, request: ModelWorkLedgerAppendRequest
) -> ModelWorkLedgerAppendReceipt | None:
    """The clash refusal already kept for this request id and these rows, else None.

    Only a request-id clash is final while the ledger holds the other rows; any other refusal (a guard whose
    cause may be gone) is not reused. A file that cannot be read is left out.
    """
    directory = outbox_dir(state_dir) / REFUSED_DIRNAME
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob(f"*_{request.request_id}.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if body.get("rows") != request.rows:
                continue
            stored = ModelWorkLedgerAppendReceipt.model_validate(body["refusal"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if stored.status is EnumWorkLedgerAppendStatus.REFUSED and (
            CLASH_MARK in stored.message
        ):
            return stored
    return None


async def drain(state_dir: Path, send: Sender, *, limit: int = 50) -> DrainResult:
    """Replay queued requests oldest first, each under its own request id."""
    directory = outbox_dir(state_dir)
    if not directory.is_dir():
        return DrainResult(0, 0, 0, 0, None)
    with (directory / DRAIN_LOCK_NAME).open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return DrainResult(
                0, 0, 0, pending_count(state_dir), "another drain holds the outbox"
            )
        replayed = duplicates = refused = 0
        stopped: str | None = None
        for path, request in pending(state_dir)[:limit]:
            if stored_refusal(state_dir, request) is not None:
                refused += 1
                settle(state_dir, request.request_id)
                path.unlink(missing_ok=True)
                continue
            try:
                receipt = await send(request)
            except (TimeoutError, OSError) as exc:
                stopped = f"request {request.request_id}: {type(exc).__name__}"
                break
            if receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED:
                replayed += 1
            elif receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE:
                duplicates += 1
            elif receipt.status is EnumWorkLedgerAppendStatus.REFUSED:
                target = directory / REFUSED_DIRNAME
                target.mkdir(exist_ok=True)
                body = json.loads(path.read_text(encoding="utf-8"))
                body["refusal"] = receipt.model_dump(mode="json")
                (target / path.name).write_text(
                    json.dumps(body, sort_keys=True), "utf-8"
                )
                refused += 1
            else:
                stopped = (
                    f"request {request.request_id} answered {receipt.status.value}"
                )
                break
            settle(state_dir, request.request_id)
            path.unlink(missing_ok=True)
        return DrainResult(
            replayed, duplicates, refused, pending_count(state_dir), stopped
        )


def _attempt_key(request: ModelWorkLedgerAppendRequest) -> str:
    """The row's words without its stamp and request id, which a retry mints anew."""
    words = _ROW_STAMP.sub("", _REQ_CELL.sub("", request.rows))
    return hashlib.sha256(f"{request.requested_by_lane}\n{words}".encode()).hexdigest()


def first_attempt(
    state_dir: Path, request: ModelWorkLedgerAppendRequest
) -> ModelWorkLedgerAppendRequest:
    """The request to send: the first unanswered attempt at the same row, else this one (recorded).

    The entry stays until ``settle`` is called with an answer, so a timeout (exit 75) or a queued append
    is retried under its original request id and rows.
    """
    directory = outbox_dir(state_dir) / ATTEMPTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_attempt_key(request)}.json"
    try:
        earlier = ModelWorkLedgerAppendRequest.model_validate_json(
            target.read_text("utf-8")
        )
    except (OSError, ValueError):
        earlier = None
    if (
        earlier is not None
        and abs(request.requested_at - earlier.requested_at) <= RETRY_WINDOW
    ):
        return earlier
    tmp = directory / f".{target.name}.{os.getpid()}.tmp"
    tmp.write_text(request.model_dump_json(), encoding="utf-8")
    tmp.replace(target)
    return request


def settle(state_dir: Path, request_id: UUID) -> None:
    """Forget an answered attempt: accepted, duplicate or refused ends the row's retries."""
    directory = outbox_dir(state_dir) / ATTEMPTS_DIRNAME
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        try:
            recorded = ModelWorkLedgerAppendRequest.model_validate_json(
                path.read_text("utf-8")
            )
        except (OSError, ValueError):
            continue
        if recorded.request_id == request_id:
            path.unlink(missing_ok=True)


async def send_once(
    state_dir: Path,
    request: ModelWorkLedgerAppendRequest,
    append: Callable[..., Awaitable[ModelWorkLedgerAppendReceipt]],
    timeout_s: float,
) -> ModelWorkLedgerAppendReceipt:
    """Send under the first unanswered attempt's id; an answer other than ``error`` ends its retries."""
    receipt = await append(first_attempt(state_dir, request), timeout_s=timeout_s)
    if receipt.status is not EnumWorkLedgerAppendStatus.ERROR:
        settle(state_dir, receipt.request_id)
    return receipt


def queued_receipt(path: Path, decision: ModelBusBindingDecision) -> dict[str, object]:
    """What the writer is told when its append is queued instead of sent."""
    return {
        "request_id": str(request_id_of(path)),
        "status": "queued",
        "exit_code": 0,
        "binding": decision.binding.value,
        "bus_state": decision.state.value,
        "outbox": path.name,
        "message": "durable bus unavailable; queued for replay under this request id",
    }


def request_id_of(path: Path) -> UUID:
    return UUID(path.stem.split("_", 1)[1])
