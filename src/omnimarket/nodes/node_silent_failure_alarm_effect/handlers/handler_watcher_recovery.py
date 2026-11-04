# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Diagnose stale complete inventories and request one bounded, locked watcher tick.

Only the watcher owns its snapshot and timestamp. A successful command or a log
line saying a handler returned never counts as a complete inventory refresh.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict

from ..models.model_watcher_recovery import (
    ModelWatcherRecoveryOptions,
    ModelWatcherRecoveryRequest,
    ModelWatcherRecoveryResult,
)


class RecoveryFacts(TypedDict):
    before_at: datetime | None
    after_at: datetime | None
    checked_at: datetime
    inventory_status: str
    log_evidence: tuple[str, ...]


def read_complete_tick(path: Path) -> datetime | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return datetime.strptime(
            str(raw.get("last_tick") or ""), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
    except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
        return None


def fresh(at: datetime | None, now: datetime) -> bool:
    return at is not None and timedelta(0) <= now - at <= timedelta(minutes=8)


class HandlerWatcherRecovery:
    def __init__(
        self,
        options: ModelWatcherRecoveryOptions,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._options, self._run = options, run

    def _commands(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        options = self._options
        if options.status_command and options.recovery_command:
            return options.status_command, options.recovery_command
        installed = plistlib.loads(options.watcher_plist.read_bytes())
        argv = installed.get("ProgramArguments")
        environment = installed.get("EnvironmentVariables", {})
        if (
            not isinstance(argv, list)
            or not all(isinstance(value, str) for value in argv)
            or argv.count("serve") != 1
        ):
            raise ValueError("installed watcher must declare one serve action")
        if not isinstance(environment, dict):
            raise ValueError("installed watcher environment is unreadable")
        action = argv.index("serve")
        if action == 0:
            raise ValueError("installed watcher has no executable prefix")
        prefix = ["env", "-u", "PYTHONPATH"]
        for key in (
            "PYTHONPATH",
            "OMNI_HOME",
            "ONEX_WORKSPACE_CONFIG_ROOT",
            "ONEX_STATE_DIR",
        ):
            value = environment.get(key)
            if isinstance(value, str) and value:
                prefix.append(f"{key}={value}")
        prefix.extend(argv[:action])
        binding: list[str] = []
        for flag in ("--bus-lane", "--omnibase-path"):
            if flag not in argv[action + 1 :] or argv.index(flag) + 1 >= len(argv):
                raise ValueError(f"installed watcher lacks {flag}")
            binding.extend([flag, argv[argv.index(flag) + 1]])
        status = (*prefix, "--status", "--state-dir", str(options.watcher_state.parent))
        recovery = (*prefix, "publish", *binding, "--wait", "--timeout", "180")
        return options.status_command or status, options.recovery_command or recovery

    def _command(self, argv: tuple[str, ...], timeout: int) -> tuple[int, str]:
        try:
            done = self._run(
                list(argv), capture_output=True, text=True, check=False, timeout=timeout
            )
            return done.returncode, " ".join((done.stdout + " " + done.stderr).split())[
                -2000:
            ]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 124, f"{type(exc).__name__}: {exc}"[:2000]

    def _evidence(self) -> tuple[str, ...]:
        options = self._options
        try:
            with options.watcher_log.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - options.log_tail_bytes)
                handle.seek(start)
                lines = (
                    handle.read(options.log_tail_bytes)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
                if start and lines:
                    lines = lines[1:]
            return tuple(
                line
                for line in lines
                if "PR-INVENTORY" in line or "tick" in line.lower()
            )[-options.log_tail_lines :]
        except OSError as exc:
            return (f"watcher log unreadable: {type(exc).__name__}",)

    def handle(
        self, request: ModelWatcherRecoveryRequest
    ) -> ModelWatcherRecoveryResult:
        started = time.monotonic()
        options = self._options
        before = read_complete_tick(options.watcher_state)
        if fresh(before, request.now):
            return ModelWatcherRecoveryResult(
                before_at=before, after_at=before, outcome="fresh"
            )
        try:
            status_command, recovery_command = self._commands()
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            plistlib.InvalidFileException,
        ) as exc:
            return ModelWatcherRecoveryResult(
                before_at=before,
                after_at=before,
                checked_at=request.now,
                outcome="refused",
                inventory_status="PR-INVENTORY unreadable from installed adapter",
                log_evidence=self._evidence(),
                detail=f"installed watcher command unreadable: {type(exc).__name__}",
            )
        status_exit, status = self._command(
            status_command, options.diagnostic_timeout_seconds
        )
        evidence = self._evidence()
        facts: RecoveryFacts = {
            "before_at": before,
            "after_at": before,
            "checked_at": request.now + timedelta(seconds=time.monotonic() - started),
            "inventory_status": f"exit={status_exit} {status}",
            "log_evidence": evidence,
        }
        if not request.apply:
            return ModelWatcherRecoveryResult(**facts, outcome="dry-run")
        lock_path = options.recovery_state.with_suffix(".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return ModelWatcherRecoveryResult(**facts, outcome="locked")
                # The prior worker may have completed between our observation and admission.
                current = read_complete_tick(options.watcher_state)
                if fresh(
                    current, request.now + timedelta(seconds=time.monotonic() - started)
                ):
                    facts["after_at"] = current
                    return ModelWatcherRecoveryResult(**facts, outcome="fresh")
                try:
                    state = json.loads(
                        options.recovery_state.read_text(encoding="utf-8")
                    )
                    last = datetime.fromisoformat(state["attempted_at"])
                    if last.tzinfo is None:
                        raise ValueError("attempt timestamp lacks timezone")
                except FileNotFoundError:
                    last = None
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    return ModelWatcherRecoveryResult(
                        **facts,
                        outcome="refused",
                        detail=f"recovery state unreadable: {type(exc).__name__}",
                    )
                if last is not None and request.now - last < timedelta(
                    seconds=options.cooldown_seconds
                ):
                    return ModelWatcherRecoveryResult(**facts, outcome="cooldown")
                # Persist admission before the external call: a timeout/restart cannot flood ticks.
                self._save(
                    {"attempted_at": request.now.isoformat(), "outcome": "attempted"}
                )
                code, detail = self._command(
                    recovery_command, options.recovery_timeout_seconds
                )
                after = read_complete_tick(options.watcher_state)
                checked_at = request.now + timedelta(seconds=time.monotonic() - started)
                # Retain the true alarm even when the command exits zero without a complete refresh.
                outcome: Literal["recovered", "attempted", "failed"] = (
                    "recovered"
                    if after is not None
                    and (before is None or after > before)
                    and fresh(after, checked_at)
                    else "attempted"
                    if code == 0
                    else "failed"
                )
                facts["after_at"] = after
                facts["checked_at"] = checked_at
                result = ModelWatcherRecoveryResult(
                    **facts,
                    outcome=outcome,
                    recovery_exit=code,
                    detail=detail,
                )
                self._save(
                    {
                        "attempted_at": request.now.isoformat(),
                        **result.model_dump(mode="json"),
                    }
                )
                return result
        except OSError as exc:
            return ModelWatcherRecoveryResult(
                **facts,
                outcome="refused",
                detail=f"recovery I/O failed: {type(exc).__name__}",
            )

    def _save(self, data: dict[str, object]) -> None:
        path = self._options.recovery_state
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, sort_keys=True)
                handle.write("\n")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
