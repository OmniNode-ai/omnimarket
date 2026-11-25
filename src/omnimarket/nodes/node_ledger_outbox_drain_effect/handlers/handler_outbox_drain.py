# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain a host's bus outbox: replay queued ledger requests, restamp a clock-guard refusal, retry refused files.

Moved from the private ledger bus writer, which is frozen. A request the bus could not take waits in the outbox; a
drain sends each one again under its own request id (the serve deduplicates on ``req=``):

* the stored request bytes go first. The serve answers a request id it already holds before it runs its clock guard, so
  a request that landed with a lost receipt is answered ``duplicate``, where a restamped copy would be refused as
  different rows;
* only a refusal naming the clock guard restamps. Every stale row takes the replay time as its stamp and names the
  original in ``late-replay-of=``; the cells that cite a restamped row by its old stamp (``closes-CLAIM=``, ``closes=``,
  the stamp inside ``id=`` and ``re=``) follow it; the restamped bytes are written back to the request file once and
  resent under the same request id, so every later replay sends those bytes first;
* accepted or duplicate removes the file; any other refusal moves it to ``refused/`` with the first 600 characters of the
  serve's message (the last 600 of stderr when there is none); any other answer stops the drain;
* with ``refused`` the drain retries the files of ``refused/`` instead, the file not tried for longest first (a retry
  rewrites the file, so a refused file that was just tried goes to the back), and ``remaining`` counts the refused files
  left.

The send is injected: ``sender(payload, request_id)`` answers a :class:`SendResult`, so the handler holds no bus client.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_ledger_outbox_drain_effect.models.model_outbox_drain import (
    ModelOutboxDrainRequest,
    ModelOutboxDrainResult,
)

OUTBOX_LOCK_DIRNAME = ".ledger_locks"
OUTBOX_SUFFIX = ".bus-outbox"
OUTBOX_REFUSED_DIRNAME = "refused"
OUTBOX_DRAIN_LOCK_NAME = ".drain.lock"
OUTBOX_EXIT_REFUSED = 65
# The serve's clock guard tolerates a stamp 24h behind its wall clock; a row older than this is restamped.
OUTBOX_RESTAMP_AFTER_S = 23 * 60 * 60
CLOCK_GUARD_TEXT = "clock guard"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REFUSAL_KEEP = 600

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_TYPES = "|".join(
    re.escape(t)
    for t in sorted(
        (
            "CLAIM",
            "STATUS",
            "TERMINAL",
            "FRICTION",
            "CORRECTION",
            "RULING",
            "OPERATOR-CONSENT",
            "MSG",
            "ACK",
            "HOLD",
            "RELEASE",
            "REPLAY",
            "STRANDED-CLONE NOTE",
        ),
        key=len,
        reverse=True,
    )
)
_ROW_STAMP = re.compile(rf"^({_STAMP}) \| (?:{_TYPES})(?= \|)")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")
_ID_CELL = re.compile(rf"^(id|re)=({_STAMP})-(.+)$")
_LATE_CELL = re.compile(rf"^late-replay-of={_STAMP}$")
_CITE_CELL = re.compile(rf"^(closes-CLAIM|closes)=({_STAMP})$")


@dataclass(frozen=True)
class SendResult:
    exit_code: int
    request_id: str
    receipt: dict[str, object] | None
    stderr: str


Sender = Callable[[str, str], SendResult]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class OutboxRequest:
    path: Path
    request_id: str
    payload: str
    reason: str
    queued_at: str
    refusal: str = ""


def outbox_dir(ledger: Path) -> Path:
    """The outbox of ``ledger``: beside its lock file, keyed on the resolved path."""
    resolved = ledger.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
    return (
        resolved.parent
        / OUTBOX_LOCK_DIRNAME
        / f"{resolved.name}.{digest}{OUTBOX_SUFFIX}"
    )


def _load(path: Path) -> OutboxRequest | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return OutboxRequest(
            path=path,
            request_id=str(body["request_id"]),
            payload=str(body["payload"]),
            reason=str(body.get("reason", "")),
            queued_at=str(body.get("queued_at", "")),
            refusal=str(body.get("refusal", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def outbox_pending(ledger: Path) -> list[OutboxRequest]:
    """The queued requests, oldest first. An unreadable file is left out."""
    directory = outbox_dir(ledger)
    if not directory.is_dir():
        return []
    return [r for p in sorted(directory.glob("*.json")) if (r := _load(p)) is not None]


def outbox_refused(ledger: Path) -> list[OutboxRequest]:
    directory = outbox_dir(ledger) / OUTBOX_REFUSED_DIRNAME
    if not directory.is_dir():
        return []
    return [r for p in sorted(directory.glob("*.json")) if (r := _load(p)) is not None]


def _stale(stamp: str, now: datetime) -> bool:
    try:
        old = datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return (now - old).total_seconds() > OUTBOX_RESTAMP_AFTER_S


def _cells_insert(row_line: str, cells: list[str]) -> str:
    """Insert ``cells`` before the free-text cell, or append them when the row has none."""
    parts = row_line.split(" | ")
    if len(parts) > 2 and not _FIELD.match(parts[-1].strip()):
        return " | ".join([*parts[:-1], *cells, parts[-1]])
    return " | ".join([*parts, *cells])


def _row_cells(row: str) -> list[str]:
    """The ``key=value`` cells of a row line, without the stamp, the type and the free text."""
    parts = row.split(" | ")
    if len(parts) > 2 and not _FIELD.match(parts[-1].strip()):
        parts = parts[:-1]
    return [part.strip() for part in parts[2:]]


def _cite_rewrite(row: str, remap: Mapping[str, str]) -> str:
    """Point the cells that name a restamped row by its old stamp (``closes-CLAIM=``/``closes=`` and the stamp inside
    ``id=``/``re=``) at its new stamp: the host matches them against the row stamp, and refuses an ``id=`` that does not
    carry its own row's stamp."""
    parts = row.split(" | ")
    last = (
        len(parts) - 1
        if len(parts) > 2 and not _FIELD.match(parts[-1].strip())
        else len(parts)
    )
    for index in range(2, last):
        cell = parts[index].strip()
        if (m := _ID_CELL.match(cell)) and m[2] in remap:
            parts[index] = f"{m[1]}={remap[m[2]]}-{m[3]}"
        elif (m := _CITE_CELL.match(cell)) and m[2] in remap:
            parts[index] = f"{m[1]}={remap[m[2]]}"
    return " | ".join(parts)


def restamp_stale(
    payload: str, now: datetime, remap: dict[str, str] | None = None
) -> str:
    """Restamp every row line older than ``OUTBOX_RESTAMP_AFTER_S`` with ``now``.

    A row line starts with a stamp and a TYPE of the ledger grammar. A stale stamp becomes ``now`` and the original goes
    into a ``late-replay-of=`` cell before the free text (kept as it is when the row has one already); continuation
    lines and fresh rows stay byte for byte. Rows that cite a restamped row by its old stamp follow it, so the citation
    still resolves; ``remap`` carries the old to new stamps of earlier requests and gains this payload's.
    """
    fresh = now.strftime(STAMP_FORMAT)
    stamps = remap if remap is not None else {}
    rows = payload.splitlines(keepends=True)
    for line in rows:
        match = _ROW_STAMP.match(line)
        if match is not None and _stale(match[1], now):
            stamps[match[1]] = fresh
    out: list[str] = []
    for line in rows:
        match = _ROW_STAMP.match(line)
        if match is None:
            out.append(line)
            continue
        text = line.rstrip("\r\n")
        ending = line[len(text) :]
        if _stale(match[1], now):
            text = fresh + text[len(match[1]) :]
            if not any(_LATE_CELL.match(cell) for cell in _row_cells(text)):
                text = _cells_insert(text, [f"late-replay-of={match[1]}"])
        out.append(_cite_rewrite(text, stamps) + ending)
    return "".join(out)


def refusal_text(result: SendResult) -> str:
    """The refusal a refused replay stores: the receipt's message when it has text (its first 600 characters), else the
    stderr tail (its last 600). Stripped."""
    message = (
        result.receipt.get("message") if isinstance(result.receipt, dict) else None
    )
    text = message.strip() if isinstance(message, str) else ""
    return (
        text[:REFUSAL_KEEP] if text else (result.stderr or "").strip()[-REFUSAL_KEEP:]
    )


def clock_guard_refused(result: SendResult) -> bool:
    """Whether the host refused on its clock guard (its refusal names it)."""
    message = (
        result.receipt.get("message") if isinstance(result.receipt, dict) else None
    )
    text = f"{message if isinstance(message, str) else ''}\n{result.stderr or ''}"
    return result.exit_code == OUTBOX_EXIT_REFUSED and CLOCK_GUARD_TEXT in text


def _write_json_atomic(path: Path, body: Mapping[str, object]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def oldest_retry_first(requests: list[OutboxRequest]) -> list[OutboxRequest]:
    """Refused requests ordered by when their file was last written: a retry rewrites the file, so ``limit``
    still-refused requests go to the back and cannot starve the rest."""
    return sorted(requests, key=lambda request: _mtime_ns(request.path))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerOutboxDrain:
    """``handle(request) -> result``: drain the pending or the refused requests of one ledger's bus outbox."""

    def __init__(
        self, sender: Sender | None = None, clock: Clock | None = None
    ) -> None:
        """``sender`` is optional: unset, the drain builds the publisher sender from the request's ``publisher_argv``.
        ``clock`` is read at each restamp (a test passes an advancing one); a request's ``now`` overrides it."""
        self._sender = sender
        self._clock = clock if clock is not None else _utc_now

    def _sender_for(self, request: ModelOutboxDrainRequest) -> Sender | None:
        if self._sender is not None:
            return self._sender
        if not request.publisher_argv:
            return None
        from omnimarket.nodes.node_ledger_outbox_drain_effect.adapters.adapter_publisher_sender import (
            PublisherSender,
        )

        return PublisherSender(
            request.publisher_argv, lane=request.lane, host=request.host
        )

    def handle(self, request: ModelOutboxDrainRequest) -> ModelOutboxDrainResult:
        ledger = Path(request.ledger_path)
        directory = outbox_dir(ledger)
        if not directory.is_dir():
            return ModelOutboxDrainResult()
        send = self._sender_for(request)
        if send is None:
            return ModelOutboxDrainResult(
                remaining=len(self._queue(ledger, request.refused)),
                stopped="no publisher: supply publisher_argv or a sender",
            )
        with (directory / OUTBOX_DRAIN_LOCK_NAME).open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ModelOutboxDrainResult(
                    remaining=len(outbox_pending(ledger)),
                    stopped="another drain holds the outbox",
                )
            return self._drain_locked(ledger, directory, request, send)

    @staticmethod
    def _queue(ledger: Path, refused: bool) -> list[OutboxRequest]:
        return outbox_refused(ledger) if refused else outbox_pending(ledger)

    def _drain_locked(
        self,
        ledger: Path,
        directory: Path,
        request: ModelOutboxDrainRequest,
        send: Sender,
    ) -> ModelOutboxDrainResult:
        target = directory / OUTBOX_REFUSED_DIRNAME
        for leftover in target.glob(".*.tmp") if target.is_dir() else ():
            leftover.unlink(
                missing_ok=True
            )  # a drain that died between write and rename
        replayed = refused_count = 0
        stopped = ""
        restamped: dict[str, str] = {}
        queue = self._queue(ledger, request.refused)
        if request.refused:
            queue = oldest_retry_first(queue)
        for item in queue[: request.limit]:
            result = send(item.payload, item.request_id)
            try:
                body = json.loads(item.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # another process settled it
            if clock_guard_refused(result):
                moment = request.now or self._clock()
                payload = restamp_stale(item.payload, moment, restamped)
                if payload != item.payload:
                    body["payload"] = payload
                    _write_json_atomic(item.path, body)
                    result = send(payload, item.request_id)
            if result.exit_code == 0:
                item.path.unlink(missing_ok=True)
                replayed += 1
                continue
            if result.exit_code == OUTBOX_EXIT_REFUSED:
                target.mkdir(exist_ok=True)
                body["refusal"] = refusal_text(result)
                _write_json_atomic(target / item.path.name, body)
                if not request.refused:
                    item.path.unlink(missing_ok=True)
                refused_count += 1
                continue
            stopped = f"request {item.request_id} answered exit {result.exit_code}"
            break
        return ModelOutboxDrainResult(
            replayed=replayed,
            refused=refused_count,
            remaining=len(self._queue(ledger, request.refused)),
            stopped=stopped,
        )
