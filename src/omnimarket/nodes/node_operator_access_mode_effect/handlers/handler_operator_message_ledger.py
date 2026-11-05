# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable arrival, reply and private handoff rows through the existing ledger bus node.

Every delivery keeps its UUID across timeouts and restarts. Only an accepted or
 duplicate ledger receipt marks it done; a queued bus receipt stays pending here.
The same handler drains on hooks and on the installer's independent host timer.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from ....handlers.operator_message_spool import (
    load_arrival_policy,
    read_operator_spool_status,
    spool_directory,
)
from ....handlers.operator_messages import (
    content_digest,
    is_operator_record,
    message_text,
    operator_message_id,
    record_identity,
    record_native_candidates,
    structural_messages,
    transcript_anchor,
)
from ....models.model_operator_message_spool import (
    ModelHookMessageRequest,
    ModelHookMessageResult,
    ModelOperatorArrivalPolicy,
)


def bus_appender(
    environ: Mapping[str, str], policy: ModelOperatorArrivalPolicy
) -> Callable[[str, str], None]:
    def append(row: str, request_id: str) -> None:
        command = shlex.split(environ.get(policy.append_command_env, ""))
        if not command:
            raise RuntimeError(f"{policy.append_command_env} is not configured")
        timeout = policy.append_timeout_seconds
        env = {
            k: v for k, v in environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}
        }
        done = subprocess.run(
            [
                *command,
                "--lane",
                policy.lane,
                "--host",
                socket.gethostname(),
                "--request-id",
                request_id,
                "--rows-file",
                "-",
                "--timeout-s",
                str(timeout),
            ],
            input=row,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        try:
            receipt = json.loads(done.stdout)
        except ValueError as exc:
            raise RuntimeError(
                f"ledger append returned no JSON receipt (exit {done.returncode}): {done.stderr}"
            ) from exc
        if done.returncode != 0 or receipt.get("status") not in {
            "accepted",
            "duplicate",
            "already-present",
        }:
            raise RuntimeError(
                f"ledger append not accepted: {receipt.get('status')} (exit {done.returncode}): {done.stderr}"
            )

    return append


def message_ledger(environ: Mapping[str, str]) -> HandlerOperatorMessageLedger:
    policy = load_arrival_policy(environ)
    return HandlerOperatorMessageLedger(
        spool_dir=spool_directory(environ, policy),
        append=bus_appender(environ, policy),
        policy=policy,
    )


class HandlerOperatorMessageLedger:
    """One locked host-wide outbox, independent of the capture consumer and session."""

    def __init__(
        self,
        *,
        spool_dir: Path,
        append: Callable[[str, str], None],
        clock: Callable[[], float] = time.time,
        policy: ModelOperatorArrivalPolicy,
    ) -> None:
        self.directory = spool_dir
        self._append = append
        self._clock = clock
        self._policy = policy
        self._bound = policy.stale_after_seconds
        self._lane = policy.lane
        self._source = policy.source

    def _queue(self, row: str, key: str, created_at: float) -> None:
        token = uuid5(NAMESPACE_URL, key).hex
        path = self.directory / f"{token}.pending.json"
        if path.exists() or (self.directory / f"{token}.done").exists():
            return
        request_id = str(uuid5(NAMESPACE_URL, key))
        stamped_row = f"{row} | req={request_id} | via=bus:{self._policy.lane} | Operator message event"
        entry = {"row": stamped_row, "request_id": request_id, "created_at": created_at}
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(entry, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        self._sync_directory()

    def _sync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rows(self) -> list[str]:
        rows: list[str] = []
        for path in [
            *self.directory.glob("*.pending.json"),
            *self.directory.glob("*.done"),
        ]:
            rows.append(str(json.loads(path.read_text(encoding="utf-8"))["row"]))
        return rows

    def _row(self, stamp: float, kind: str, session: str, **cells: str) -> str:
        prefix = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = {
            "lane": self._lane,
            "kind": kind,
            "session": session,
            "source": self._source,
            **cells,
        }
        return (
            prefix
            + " | STATUS | "
            + " | ".join(f"{k}={quote(v, safe='-._,:')}" for k, v in fields.items())
        )

    def handle(self, request: ModelHookMessageRequest) -> ModelHookMessageResult:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        now = self._clock()
        appended = 0
        errors: list[str] = []
        payload = request.payload
        session = str(payload.get("session_id") or "")
        records: list[dict[str, Any]] = []
        transcript = str(payload.get("transcript_path") or "")
        try:
            if transcript and Path(transcript).is_file():
                for line in Path(transcript).read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        records.append(row)
        except OSError as exc:
            errors.append(f"transcript: {type(exc).__name__}: {exc}")
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if request.event == "prompt" and session:
                candidate = {
                    "type": "user",
                    "message": {"content": payload.get("prompt")},
                    "isSidechain": payload.get("isSidechain", False),
                    "isMeta": payload.get("isMeta", False),
                }
                text = message_text(candidate)
                if is_operator_record(candidate):
                    native = str(payload.get("message_id") or payload.get("uuid") or "")
                    if (
                        not native
                        and records
                        and is_operator_record(records[-1])
                        and message_text(records[-1]) == text
                    ):
                        native = record_identity(records[-1], len(records) - 1)
                    if not native:
                        anchor = (
                            transcript_anchor(records)
                            if records
                            else str(payload.get("timestamp") or "start")
                        )
                        native = f"{anchor}:{content_digest(text)}"
                    message_id = operator_message_id(session, native)
                    # A replay can see the user record that was not yet persisted
                    # during the first hook. Retain the earlier arrival's identity.
                    identities = {quote(native, safe="-._,:")}
                    if (
                        not (payload.get("message_id") or payload.get("uuid"))
                        and records
                        and is_operator_record(records[-1])
                        and message_text(records[-1]) == text
                    ):
                        identities.update(
                            quote(value, safe="-._,:")
                            for value in record_native_candidates(
                                records, len(records) - 1
                            )
                        )
                    for cached in self._rows():
                        cells = dict(
                            cell.split("=", 1)
                            for cell in cached.split(" | ")[2:]
                            if "=" in cell
                        )
                        if (
                            cells.get("kind") == "operator-message-arrival"
                            and cells.get("session") == quote(session, safe="-._,:")
                            and cells.get("native_id") in identities
                        ):
                            message_id = cells["item"]
                            break
                    row = self._row(
                        now,
                        "operator-message-arrival",
                        session,
                        item=message_id,
                        item_state="open",
                        native_id=native,
                        content_sha256=content_digest(text),
                    )
                    self._queue(row, f"arrival:{message_id}", now)
            if request.event == "stop" and session:
                pending, covered = structural_messages(records)
                arrivals: list[dict[str, str]] = []
                for row in self._rows():
                    cells = dict(
                        cell.split("=", 1)
                        for cell in row.split(" | ")[2:]
                        if "=" in cell
                    )
                    if cells.get("kind") == "operator-message-arrival" and cells.get(
                        "session"
                    ) == quote(session, safe="-._,:"):
                        arrivals.append(cells)
                matched: set[str] = set()
                covered_indices = {index for index, _ in covered}
                answers: list[str] = []
                for index, record in sorted(
                    [*pending, *covered], key=lambda pair: pair[0]
                ):
                    native = record_identity(record, index)
                    native_ids = {
                        quote(value, safe="-._,:")
                        for value in record_native_candidates(records, index)
                    }
                    text = message_text(record)
                    message_id = operator_message_id(session, native)
                    match = next(
                        (
                            a
                            for a in arrivals
                            if a["item"] not in matched
                            and (
                                a.get("native_id") in native_ids
                                or a["item"] == message_id
                            )
                        ),
                        None,
                    )
                    if match:
                        message_id = match["item"]
                        matched.add(message_id)
                    if index in covered_indices:
                        answers.append(message_id)
                    else:
                        self._queue(
                            self._row(
                                now,
                                "operator-message-acknowledgement",
                                session,
                                item=message_id,
                                words=text,
                                item_state="open",
                            ),
                            f"acknowledgement:{message_id}",
                            now,
                        )
                if answers:
                    reply_id = (
                        record_identity(records[-1], len(records) - 1)
                        if records
                        else content_digest(
                            str(payload.get("last_assistant_message") or "")
                        )
                    )
                    self._queue(
                        self._row(
                            now,
                            "operator-message-reply",
                            session,
                            answers=",".join(answers),
                        ),
                        f"reply:{session}:{reply_id}",
                        now,
                    )
        # Network delivery never holds the enqueue lock: a slow timer cannot lose a
        # new prompt to the hook's deadline. Concurrent hooks only attempt the drain.
        with (self.directory / ".drain.lock").open("a") as drain_lock:
            try:
                fcntl.flock(drain_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ModelHookMessageResult(
                    status=read_operator_spool_status(
                        self.directory, now=now, bound_seconds=self._bound
                    )
                )
            # Try every pending row, oldest first. One unavailable bus stops this invocation;
            # the timer retries with the identical request id after the bus recovers.
            paths = sorted(
                self.directory.glob("*.pending.json"),
                key=lambda p: p.stat().st_mtime_ns,
            )
            limit = (
                self._policy.max_appends_per_drain
                if request.event == "drain"
                else self._policy.max_appends_per_hook
            )
            for path in paths[:limit]:
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    self._append(entry["row"], entry["request_id"])
                    path.replace(
                        path.with_name(
                            path.name.removesuffix(".pending.json") + ".done"
                        )
                    )
                    self._sync_directory()
                    appended += 1
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                    break
            status = read_operator_spool_status(
                self.directory, now=now, bound_seconds=self._bound
            )
        if errors:
            from dataclasses import replace

            status = replace(status, errors=(*status.errors, *errors))
        return ModelHookMessageResult(status=status, appended=appended)
