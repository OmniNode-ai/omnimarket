# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable local outbox for ledger appends made while the bus binding is degraded (OMN-20275).

An append on the in-memory binding has no ledger host to reach. It is written here instead: one JSON file
per request, written to a temporary name, fsynced and renamed, so a reader never sees half a request.
The request id is the idempotency key the ledger host's serve deduplicates on, so a replay of a request
whose first attempt did land is answered ``duplicate`` and the row is appended once. Accepted or
duplicate removes the file; a refusal is final and moves it to ``refused/``; any other answer stops the
drain and leaves the file for the next one. One drain at a time (an fcntl lock).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

OUTBOX_DIRNAME = "work-ledger-outbox"
REFUSED_DIRNAME = "refused"
DRAIN_LOCK_NAME = ".drain.lock"

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
            path.unlink(missing_ok=True)
        return DrainResult(
            replayed, duplicates, refused, pending_count(state_dir), stopped
        )


def request_id_of(path: Path) -> UUID:
    return UUID(path.stem.split("_", 1)[1])
