# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle the bus-outbox requests a replay moved to ``refused/`` (moved from the private ledger bus writer).

A refusal used to be final: the request sat in ``refused/`` and the bus check reported it as a blocker forever, with only
the publisher's status line as its text. A settle rules on each refused request:

* the host may have refused a request the serve now takes (the gate it failed was lifted, the config it lacked is back):
  accepted or duplicate settles it, leaves a receipt in ``settled/`` holding the refused body and removes the file;
* a stamp older than the serve's clock-guard tolerance was a queued row replayed a day late, the one refusal a host may
  mend on its own, by sending the row again restamped and re-identified with the original marked;
* a row-id clash is a person's ruling, never the host's; any other refusal of a fresh-stamped row is the serve refusing
  the row itself. Both keep the file, with the serve's latest message as its refusal text.

The send is injected: ``sender(payload, request_id)`` answers a :class:`SendResult` (the publisher's exit code, its
receipt and its stderr), so the node holds no bus client of its own.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_ledger_outbox_settle_effect.models.model_outbox_settle import (
    ModelOutboxSettleRequest,
    ModelOutboxSettleResult,
)

OUTBOX_LOCK_DIRNAME = ".ledger_locks"
OUTBOX_SUFFIX = ".bus-outbox"
OUTBOX_REFUSED_DIRNAME = "refused"
OUTBOX_SETTLED_DIRNAME = "settled"
OUTBOX_DRAIN_LOCK_NAME = ".drain.lock"
EXIT_REFUSED = 65
CLASH_TEXT = "already belongs to different rows"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")


@dataclass(frozen=True)
class SendResult:
    exit_code: int
    request_id: str
    receipt: dict[str, object] | None
    stderr: str


Sender = Callable[[str, str], SendResult]


@dataclass(frozen=True)
class OutboxRequest:
    path: Path
    request_id: str
    payload: str
    refusal: str


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
            refusal=str(body.get("refusal", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def outbox_refused(ledger: Path) -> list[OutboxRequest]:
    directory = outbox_dir(ledger) / OUTBOX_REFUSED_DIRNAME
    if not directory.is_dir():
        return []
    return [r for p in sorted(directory.glob("*.json")) if (r := _load(p)) is not None]


def refusal_text(result: SendResult) -> str:
    """The serve's own refusal text: the receipt's ``message`` when it carries one, else the publisher's stderr tail."""
    if isinstance(result.receipt, dict):
        message = result.receipt.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[-600:]
    return (result.stderr or "").strip()[-600:]


def settle_request_id(old_request_id: str) -> str:
    """The id a settled resend goes under: uuid5 of the old one, so a retry whose receipt was lost answers duplicate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"onex-ledger-settle:{old_request_id}"))


def _cells_insert(row_line: str, cells: list[str]) -> str:
    """Insert ``cells`` before the free-text cell, or append them when the row has none."""
    parts = row_line.split(" | ")
    if len(parts) > 2 and not _FIELD.match(parts[-1].strip()):
        return " | ".join([*parts[:-1], *cells, parts[-1]])
    return " | ".join([*parts, *cells])


def restamp_rows(payload: str, old_request_id: str, now: datetime) -> str:
    """Return ``payload`` with every row-start line stamped ``now`` and re-identified as the resend of ``old_request_id``.

    Each row-start line's leading stamp becomes ``now``'s, its ``req=<old id>`` cell becomes the new id, and the cells
    ``resent-from=<old id>`` and ``original-stamp=<old stamp>`` are inserted before its free text. Continuation lines
    are unchanged, and a trailing newline is kept as it was.
    """
    new_id = settle_request_id(old_request_id)
    stamped_now = now.strftime(STAMP_FORMAT)
    out: list[str] = []
    for line in payload.splitlines():
        if _ROW_START.match(line):
            cells = line.split(" | ")
            old_stamp = cells[0]
            cells[0] = stamped_now
            cells = [
                f"req={new_id}" if cell == f"req={old_request_id}" else cell
                for cell in cells
            ]
            line = _cells_insert(
                " | ".join(cells),
                [f"resent-from={old_request_id}", f"original-stamp={old_stamp}"],
            )
        out.append(line)
    return "\n".join(out) + ("\n" if payload.endswith("\n") else "")


def all_row_stamps_stale(payload: str, now: datetime, stale_after_s: float) -> bool:
    """Whether every row-start line is older than the stale bound, so the serve's clock guard is the likely refusal.

    A stamp that will not parse, or no row-start line at all, is not stale: only a request the guard certainly rejected
    is resent restamped.
    """
    bound = now - timedelta(seconds=stale_after_s)
    starts = 0
    for line in payload.splitlines():
        if not _ROW_START.match(line):
            continue
        starts += 1
        try:
            stamped = datetime.strptime(line[:20], STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return False
        if stamped >= bound:
            return False
    return starts > 0


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to a temporary name, fsync and rename it, so a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _mtime(path: Path) -> float | None:
    """The file's modification time, or None when it is gone (another process settled it)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_body(path: Path) -> dict[str, object] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _rewrite_refusal(request: OutboxRequest, message: str) -> None:
    body = _read_body(request.path)
    if body is None:
        return
    body["refusal"] = message[-600:]
    _write_atomic(request.path, json.dumps(body, sort_keys=True))


def _settle_receipt(
    directory: Path,
    request: OutboxRequest,
    body: dict[str, object],
    how: str,
    now: datetime,
) -> None:
    target = directory / OUTBOX_SETTLED_DIRNAME
    target.mkdir(exist_ok=True)
    record: dict[str, object] = {
        "request_id": request.request_id,
        "how": how,
        "settled_at": now.strftime(STAMP_FORMAT),
        "original": body,
    }
    _write_atomic(target / request.path.name, json.dumps(record, sort_keys=True))


def settle_due(ledger: Path, now: datetime, retry_interval_s: float) -> bool:
    """Whether a refused request has waited out the retry interval since its last refusal."""
    return any(
        (modified := _mtime(request.path)) is not None
        and now.timestamp() - modified >= retry_interval_s
        for request in outbox_refused(ledger)
    )


class HandlerOutboxSettle:
    """``handle(request) -> result``: settle the refused requests of one ledger's bus outbox."""

    def __init__(self, sender: Sender) -> None:
        self._send = sender

    def handle(self, request: ModelOutboxSettleRequest) -> ModelOutboxSettleResult:
        ledger = Path(request.ledger_path)
        moment = request.now or datetime.now(UTC)
        if request.operation == "due":
            return ModelOutboxSettleResult(
                operation="due",
                due=settle_due(ledger, moment, request.retry_interval_s),
                remaining=len(outbox_refused(ledger)),
            )
        directory = outbox_dir(ledger)
        if not outbox_refused(ledger):
            return ModelOutboxSettleResult(operation="settle")
        with (directory / OUTBOX_DRAIN_LOCK_NAME).open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ModelOutboxSettleResult(
                    operation="settle",
                    remaining=len(outbox_refused(ledger)),
                    stopped="another drain holds the outbox",
                )
            return self._settle_locked(ledger, directory, request, moment)

    def _settle_locked(
        self,
        ledger: Path,
        directory: Path,
        request: ModelOutboxSettleRequest,
        moment: datetime,
    ) -> ModelOutboxSettleResult:
        settled = resent = refused = 0
        stopped = ""
        for item in outbox_refused(ledger):
            modified = _mtime(item.path)
            body = _read_body(item.path)
            if modified is None or body is None:
                continue
            if moment.timestamp() - modified < request.min_age_s:
                continue
            result = self._send(item.payload, item.request_id)
            if result.exit_code == 0:
                status = (
                    result.receipt.get("status")
                    if isinstance(result.receipt, dict)
                    else None
                )
                how = status if isinstance(status, str) and status else "accepted"
                _settle_receipt(directory, item, body, how, moment)
                item.path.unlink(missing_ok=True)
                settled += 1
                continue
            if result.exit_code != EXIT_REFUSED:
                stopped = f"request {item.request_id} answered exit {result.exit_code}"
                break
            message = refusal_text(result)
            if CLASH_TEXT in message:
                _rewrite_refusal(item, message)
                refused += 1
                continue
            if all_row_stamps_stale(item.payload, moment, request.stale_stamp_age_s):
                new_id = settle_request_id(item.request_id)
                payload = restamp_rows(item.payload, item.request_id, moment)
                resend = self._send(payload, new_id)
                if resend.exit_code == 0:
                    _settle_receipt(directory, item, body, "resent-restamped", moment)
                    item.path.unlink(missing_ok=True)
                    settled += 1
                    resent += 1
                    continue
                if resend.exit_code == EXIT_REFUSED:
                    _rewrite_refusal(item, refusal_text(resend))
                    refused += 1
                    continue
                stopped = f"request {new_id} answered exit {resend.exit_code}"
                break
            _rewrite_refusal(item, message)
            refused += 1
        return ModelOutboxSettleResult(
            operation="settle",
            settled=settled,
            resent=resent,
            refused=refused,
            remaining=len(outbox_refused(ledger)),
            stopped=stopped,
        )
