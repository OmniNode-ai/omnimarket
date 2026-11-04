# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repair only the independently observed safe cause, retaining every quiet-host alarm."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerRequest,
    ModelQuietHostRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerDrainerAlarm,
    HandlerQuietHostAlarm,
)

from ..models.model_quiet_host_repair import (
    ModelHostCaptureRepair,
    ModelQuietHostRepairResult,
)
from ..protocols import ProtocolDrainerWatch

ReadEvents = Callable[[], tuple[dict[str, datetime], str]]


class _Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempted_at: AwareDatetime


class QuietHostRepairPlanner:
    def __init__(
        self,
        *,
        watches: Mapping[str, ProtocolDrainerWatch],
        read_events: ReadEvents,
        state_file: Path | None = None,
        cooldown_minutes: int = 120,
        max_repairs: int = 8,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._watches = watches
        self._read_events = read_events
        self._path = state_file
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._max_repairs = max_repairs
        self._clock = clock
        self._attempts: dict[str, _Attempt] = {}

    def handle(
        self, request: ModelQuietHostRequest, *, apply: bool
    ) -> ModelQuietHostRepairResult:
        decision = HandlerQuietHostAlarm().handle(request)
        if not decision.alarm:
            return ModelQuietHostRepairResult(decision=decision)
        if not apply or self._path is None:
            try:
                return self._handle(request, apply=apply)
            except (OSError, ValueError) as exc:
                return ModelQuietHostRepairResult(
                    decision=decision,
                    warnings=(f"capture repair state unreadable: {exc}",),
                )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return ModelQuietHostRepairResult(
                        decision=decision,
                        warnings=("capture repair locked by another tick",),
                    )
                return self._handle(request, apply=True)
        except (OSError, ValueError) as exc:
            return ModelQuietHostRepairResult(
                decision=decision, warnings=(f"capture repair state unreadable: {exc}",)
            )

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            payload = json.loads(self._path.read_text())
        except FileNotFoundError:
            self._attempts = {}
            return
        if not isinstance(payload, dict):
            raise ValueError("capture repair state must be a host map")
        self._attempts = {
            host: _Attempt.model_validate(value) for host, value in payload.items()
        }

    def _save(self) -> None:
        if self._path is None:
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._path.parent, delete=False
            ) as out:
                temporary = Path(out.name)
                json.dump(
                    {
                        host: attempt.model_dump(mode="json")
                        for host, attempt in self._attempts.items()
                    },
                    out,
                )
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self._path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _handle(
        self, request: ModelQuietHostRequest, *, apply: bool
    ) -> ModelQuietHostRepairResult:
        decision = HandlerQuietHostAlarm().handle(request)
        self._load()
        repairs: list[ModelHostCaptureRepair] = []
        warnings: list[str] = []
        attempted = 0
        for host in decision.quiet_hosts:
            watch = self._watches.get(host)
            state = "unconfigured"
            detail = "no declared observation/installer binding for this host"
            if watch is None:
                repairs.append(
                    ModelHostCaptureRepair(
                        host=host, state="unconfigured", detail=detail
                    )
                )
                continue
            try:
                before = watch.observe()
                verdict = HandlerDrainerAlarm().handle(
                    ModelDrainerRequest(now=request.now, host=host, before=before)
                )
                if before.job_state == "unknown":
                    state, detail = "unknown", before.job_detail
                elif not verdict.alarm:
                    state, detail = (
                        "not-needed",
                        "drainer is live; missing capture has another cause",
                    )
                elif not verdict.repair_safe:
                    state, detail = "not-safe", verdict.fix
                elif not apply:
                    state, detail = (
                        "dry-run",
                        "safe repair observed; no installer called",
                    )
                elif (
                    (previous := self._attempts.get(host)) is not None
                    and self._clock() - previous.attempted_at < self._cooldown
                ):
                    state, detail = (
                        "cooldown",
                        "previous installer attempt is inside the per-host cooldown",
                    )
                elif attempted >= self._max_repairs:
                    state, detail = "limited", "per-tick installer limit reached"
                else:
                    at = self._clock()
                    self._attempts[host] = _Attempt(attempted_at=at)
                    # Persist before invoking the installer, including failed attempts and process crashes.
                    self._save()
                    attempted += 1
                    repair = watch.repair()
                    repairs.append(
                        ModelHostCaptureRepair(
                            host=host,
                            state="pending-events" if repair.succeeded else "failed",
                            attempted_at=at,
                            detail=repair.detail,
                        )
                    )
                    continue
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                state, detail = "unknown", str(exc)
                warnings.append(f"{host}: capture repair failed: {exc}")
            repairs.append(
                ModelHostCaptureRepair(host=host, state=state, detail=detail)
            )
        if any(item.state == "pending-events" for item in repairs):
            try:
                events, problem = self._read_events()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                events, problem = {}, str(exc)
            if problem:
                warnings.append(f"capture repair readback unreadable: {problem}")
            read_at = self._clock()
            original = {host.host: host.last_event_at for host in request.hosts}
            for index, item in enumerate(repairs):
                event = events.get(item.host)
                prior = original.get(item.host)
                if (
                    not problem
                    and item.state == "pending-events"
                    and item.attempted_at is not None
                    and event is not None
                    and item.attempted_at < event <= read_at
                    and read_at - event <= timedelta(minutes=request.quiet_minutes)
                    and (prior is None or event > prior)
                ):
                    repairs[index] = item.model_copy(
                        update={"state": "recovered", "event_at": event}
                    )
        # Installation/readback evidence never rewrites the triggering decision or its FRICTION cells.
        return ModelQuietHostRepairResult(
            decision=decision, repairs=tuple(repairs), warnings=tuple(warnings)
        )
