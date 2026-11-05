# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded per-message Stop gate, with a durable acknowledgement before EndTurn.

The transcript and independent arrival outbox are authoritative. Capture's list is
never an input. The answer service owns the atomic cause claim and lane start;
this gate cannot turn a queued dispatch or a bare claim into running evidence.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote

from ....handlers.operator_message_spool import (
    load_arrival_policy,
    spool_directory,
)
from ..models.model_unanswered_guard_request import (
    ModelAnswerHandoffRequest,
    ModelAnswerHandoffResult,
    ModelGuardRow,
    ModelGuardSnapshot,
    ModelUnansweredGuardPolicy,
    ModelUnansweredGuardRequest,
    ModelUnansweredGuardResult,
)

POLICY_PATH = Path(__file__).parents[1] / "unanswered_guard.overlay.json"


def load_guard_policy(environ: Mapping[str, str]) -> ModelUnansweredGuardPolicy:
    defaults = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    override = environ.get(defaults["overlay_env"])
    if override:
        defaults.update(json.loads(Path(override).read_text(encoding="utf-8")))
    return ModelUnansweredGuardPolicy(**defaults)


class ProtocolGuardSnapshot(Protocol):
    def __call__(self, request: ModelUnansweredGuardRequest) -> ModelGuardSnapshot: ...


class ProtocolAnswerHandoff(Protocol):
    def __call__(
        self, request: ModelAnswerHandoffRequest, timeout: float
    ) -> ModelAnswerHandoffResult: ...


class ProtocolGuardRows(Protocol):
    def append(self, row: ModelGuardRow, key: str) -> bool: ...


class GuardRows:
    """Private, fsynced rows in the hook's existing durable outbox.

    Acknowledgement durability is local and synchronous; the existing independent
    drain sends the same UUID to the ledger node. An alarm retry writes one row.
    """

    def __init__(
        self, environ: Mapping[str, str], policy: ModelUnansweredGuardPolicy
    ) -> None:
        self.directory = spool_directory(environ, load_arrival_policy(environ))
        self.policy = policy
        self.lane = load_arrival_policy(environ).lane

    def append(self, row: ModelGuardRow, key: str) -> bool:
        from uuid import NAMESPACE_URL, uuid5

        token = uuid5(NAMESPACE_URL, key)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = self.directory / f"{token.hex}.pending.json"
        done = self.directory / f"{token.hex}.done"
        with (self.directory / ".lock").open("a") as lock:
            _acquire_lock(
                lock.fileno(),
                time.monotonic() + self.policy.budget_ms / 1000,
                self.policy.counter_poll_seconds,
            )
            if pending.exists() or done.exists():
                existing = json.loads(
                    (pending if pending.exists() else done).read_text()
                )
                # Only an identical acknowledgement is evidence, never an earlier
                # content-capture row which did not name the answer owner.
                return bool(
                    existing.get("guard_row")
                    == {
                        "kind": row.kind,
                        "owner": row.owner,
                        "cause": row.cause,
                        "message_ids": list(row.message_ids),
                    }
                )
            cells = {
                "lane": self.lane,
                "kind": row.kind,
                "session": row.session_id,
                "source": self.policy.source,
                "item": ",".join(row.message_ids),
                "owner": row.owner,
                "cause": row.cause,
                "words": row.words or "",
                "detail": row.detail,
            }
            text = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                + " | STATUS | "
                + " | ".join(
                    f"{name}={quote(value, safe='-._,:')}"
                    for name, value in cells.items()
                )
            )
            entry = {
                "row": text
                + f" | req={token} | via=bus:{self.lane} | Operator Stop gate",
                "request_id": str(token),
                "created_at": time.time(),
                "guard_row": {
                    "kind": row.kind,
                    "owner": row.owner,
                    "cause": row.cause,
                    "message_ids": list(row.message_ids),
                },
            }
            temporary = pending.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(entry, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(pending)
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return True


def arrival_rows(environ: Mapping[str, str]) -> tuple[str, ...]:
    directory = spool_directory(environ, load_arrival_policy(environ))
    # Read without the enqueue lock: atomic rename makes each individual row whole.
    rows: dict[str, str] = {}
    for path in [*directory.glob("*.pending.json"), *directory.glob("*.done")]:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # A concurrent drain renamed it; retry its .done name below.
        rows[str(entry["request_id"])] = str(entry["row"])
    for path in directory.glob("*.done"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        rows[str(entry["request_id"])] = str(entry["row"])
    return tuple(rows.values())


class HandlerUnansweredGuard:
    def __init__(
        self,
        *,
        snapshot: ProtocolGuardSnapshot | None = None,
        handoff: ProtocolAnswerHandoff | None = None,
        rows: ProtocolGuardRows | None = None,
        policy: ModelUnansweredGuardPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.snapshot = snapshot
        self.handoff = handoff
        self.rows = rows
        self.policy = policy
        self.clock = clock

    async def handle_async(
        self, request: ModelUnansweredGuardRequest
    ) -> ModelUnansweredGuardResult:
        import asyncio

        # A hosted bus callback must not nest the synchronous hook's event loop.
        return await asyncio.to_thread(self.handle, request)

    def handle(
        self, request: ModelUnansweredGuardRequest
    ) -> ModelUnansweredGuardResult:
        from .handler_unanswered_guard_runtime import (
            HandlerUnansweredSnapshot,
            answer_handoff,
        )

        policy = self.policy or load_guard_policy(request.environ)
        rows = self.rows or GuardRows(request.environ, policy)
        start = self.clock()
        messages: tuple[tuple[str, str | None], ...] = ()
        try:
            if request.silence_tick:
                return ModelUnansweredGuardResult(action="EndTurn")
            snapshot = (self.snapshot or HandlerUnansweredSnapshot().handle)(request)
            messages = snapshot.unanswered
            if (self.clock() - start) * 1000 >= policy.budget_ms:
                raise TimeoutError("Stop gate snapshot exceeded budget")
            if not messages:
                return ModelUnansweredGuardResult(action="EndTurn")
            blocked: list[str] = []
            handed: list[str] = []
            with _counter_state(
                Path(request.state_dir),
                request.session_id,
                max(0, policy.budget_ms / 1000 - (self.clock() - start)),
                policy.counter_poll_seconds,
            ) as state:
                for message_id, words in messages:
                    if (self.clock() - start) * 1000 >= policy.budget_ms:
                        raise TimeoutError("Stop gate budget exhausted")
                    count = int(state.get(message_id, 0))
                    if count < policy.max_blocks:
                        state[message_id] = count + 1
                        blocked.append(message_id)
                        continue
                    cause = f"answer@{request.session_id}@{message_id}"
                    timeout = min(
                        policy.handoff_timeout_seconds,
                        policy.budget_ms / 1000 - (self.clock() - start),
                    )
                    result = (self.handoff or answer_handoff(request.environ))(
                        ModelAnswerHandoffRequest(
                            session_id=request.session_id,
                            message_id=message_id,
                            cause=cause,
                            words=words,
                        ),
                        timeout,
                    )
                    if self.clock() - start >= policy.budget_ms / 1000:
                        raise TimeoutError("Stop gate handoff exceeded budget")
                    if result.cause != cause or not (
                        result.claim_id
                        and result.lane_id
                        and result.owner
                        and result.running
                    ):
                        blocked.append(message_id)
                        continue
                    ack = ModelGuardRow(
                        policy.acknowledgement_kind,
                        request.session_id,
                        (message_id,),
                        result.owner,
                        cause,
                        words,
                    )
                    key = f"stop-ack:{cause}:{result.claim_id}:{result.lane_id}"
                    if not rows.append(ack, key):
                        blocked.append(message_id)
                        continue
                    handed.append(message_id)
            if (self.clock() - start) * 1000 >= policy.budget_ms:
                raise TimeoutError("Stop gate acknowledgement exceeded budget")
            if blocked:
                return ModelUnansweredGuardResult(
                    action="GateBlock",
                    blocked_ids=tuple(blocked),
                    handed_off_ids=tuple(handed),
                    reason="Unanswered operator messages: "
                    + ", ".join(blocked)
                    + ". Reply before ending this turn.",
                )
            return ModelUnansweredGuardResult(
                action="GateHandoff", handed_off_ids=tuple(handed)
            )
        except Exception as exc:
            # A failed transcript still has the independent harness arrivals. A
            # broken arrival store must not hide the alarm itself.
            if not messages:
                try:
                    ids = _arrival_ids(
                        arrival_rows(request.environ), request.session_id
                    )
                except Exception:
                    ids = ()
            else:
                ids = tuple(message_id for message_id, _ in messages)
            named = (
                ", ".join(ids)
                if ids
                else f"unknown messages in session {request.session_id}"
            )
            alarm = f"[unanswered-stop] ALARM: {named}: {type(exc).__name__}: {exc}. Turn allowed; answer coverage could not be verified."
            key = f"stop-alarm:{request.session_id}:" + ",".join(ids)
            try:
                rows.append(
                    ModelGuardRow(
                        policy.alarm_kind, request.session_id, ids, detail=alarm
                    ),
                    key,
                )
            except Exception as row_exc:
                alarm += (
                    f" Alarm outbox unavailable ({type(row_exc).__name__}: {row_exc})."
                )
            return ModelUnansweredGuardResult(action="Alarm", alarm=alarm)


def _arrival_ids(rows: tuple[str, ...], session: str) -> tuple[str, ...]:
    arrivals: set[str] = set()
    answered: set[str] = set()
    for row in rows:
        cells = {
            k: unquote(v)
            for cell in row.split(" | ")[2:]
            if "=" in cell
            for k, v in [cell.split("=", 1)]
        }
        if cells.get("session") != session:
            continue
        if cells.get("kind") == "operator-message-arrival":
            arrivals.add(cells["item"])
        elif cells.get("kind") == "operator-message-reply":
            answered.update(cells.get("answers", "").split(","))
    return tuple(sorted(arrivals - answered))


@contextmanager
def _counter_state(
    directory: Path, session: str, timeout: float, poll_seconds: float
) -> Iterator[dict[str, int]]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = hashlib.sha256(session.encode()).hexdigest()
    path = directory / f"unanswered-{token}.json"
    deadline = time.monotonic() + timeout
    with path.with_suffix(".lock").open("a") as lock:
        _acquire_lock(lock.fileno(), deadline, poll_seconds)
        state = json.loads(path.read_text()) if path.exists() else {}
        try:
            yield state
        finally:
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)


def _acquire_lock(fd: int, deadline: float, poll_seconds: float) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Stop gate state lock timed out") from None
            time.sleep(min(poll_seconds, remaining))
