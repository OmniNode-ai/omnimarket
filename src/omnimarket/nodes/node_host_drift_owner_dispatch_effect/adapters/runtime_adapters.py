# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime backend: the checker's state file, the ledger read over the bus, files for the record and briefs, a detached runner."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOpenItem,
    ModelDriftState,
    ModelLiveLanes,
    ModelPlaceOutcome,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    DriftStateUnreadableError,
    LiveLanesUnreadableError,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class FileDriftOpenItems:
    """Reads the checker's ``state.json``: ``{"host": str, "open": [{key, kind, declared, observed, first_seen_at, bound_seconds, verdict}]}``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> ModelDriftState:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            host = str(document["host"])
            items = tuple(
                ModelDriftOpenItem(host=host, **{k: row[k] for k in _ITEM_FIELDS})
                for row in document["open"]
            )
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise DriftStateUnreadableError(
                f"{self._path}: {type(exc).__name__}"
            ) from exc
        return ModelDriftState(host=host, items=items)


_ITEM_FIELDS = (
    "key",
    "kind",
    "declared",
    "observed",
    "first_seen_at",
    "bound_seconds",
    "verdict",
)


class FileDispatchRecord:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, str]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {str(k): str(v) for k, v in document.items()}

    def save(self, record: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            temp = Path(handle.name)
        temp.replace(self._path)


class FileBriefWriter:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def write(self, name: str, text: str) -> str:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class LedgerQueryLiveLanes:
    """Reads the lanes live for a cause from the rolling ledger over the bus (``onex ledger query``).

    A lane covers a cause when a CLAIM row of the lookback window names the cause key and no TERMINAL row
    of that lane follows. The host's own ledger file is not read: on a lab host it is a stale copy.
    """

    def __init__(
        self,
        query_command: tuple[str, ...],
        lookback_hours: float,
        row_limit: int,
        timeout_seconds: float,
        clock: Callable[[], datetime],
        run: Run = subprocess.run,
    ) -> None:
        self._command = query_command
        self._lookback = timedelta(hours=lookback_hours)
        self._limit = row_limit
        self._timeout = timeout_seconds
        self._clock = clock
        self._run = run

    def _rows(self, *flags: str) -> list[dict[str, str]]:
        since = (self._clock() - self._lookback).astimezone(UTC).strftime(_STAMP)
        argv = [
            *self._command,
            "--query",
            "rows",
            "--since",
            since,
            "--limit",
            str(self._limit),
            *flags,
        ]
        try:
            done = self._run(
                argv, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LiveLanesUnreadableError(
                f"ledger query: {type(exc).__name__}"
            ) from exc
        payload = next(
            (
                line
                for line in reversed(done.stdout.splitlines())
                if line.startswith("{")
            ),
            None,
        )
        if done.returncode != 0 or payload is None:
            reason = _last_line(done.stderr or done.stdout or "")
            raise LiveLanesUnreadableError(
                f"ledger query exited {done.returncode}: {reason}"
            )
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise LiveLanesUnreadableError("ledger query answered no JSON") from exc
        if answer.get("error") or answer.get("refusal"):
            raise LiveLanesUnreadableError(
                f"ledger query refused: {answer.get('error') or answer.get('refusal')}"
            )
        return [
            {
                "lane": str(row.get("row_lane") or ""),
                "type": str(row.get("row_type") or ""),
                "text": str(row.get("text") or ""),
            }
            for row in answer.get("rows") or []
        ]

    def read(self, cause_keys: tuple[str, ...]) -> ModelLiveLanes:
        causes: list[str] = []
        lanes: list[str] = []
        for key in cause_keys:
            for row in self._rows("--kind", "CLAIM", "--term", key):
                if row["type"] != "CLAIM" or key not in row["text"] or not row["lane"]:
                    continue
                if self._rows("--kind", "TERMINAL", "--lane", row["lane"]):
                    continue
                causes.append(key)
                lanes.append(row["lane"])
        return ModelLiveLanes(
            covered_causes=tuple(dict.fromkeys(causes)),
            covered_lanes=tuple(dict.fromkeys(lanes)),
        )


class SubprocessLaneRunner:
    """Starts one lane detached; a runner still running after the settle window is a lane that started."""

    def __init__(self, settle_seconds: float, log_directory: Path) -> None:
        self._settle = settle_seconds
        self._logs = log_directory

    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        try:
            self._logs.mkdir(parents=True, exist_ok=True)
            log = (
                self._logs / f"{Path(argv[argv.index('--lane') + 1]).name}-dispatch.log"
            )
            with log.open("ab") as output:
                runner = subprocess.Popen(
                    list(argv),
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    env=dict(os.environ),
                )
            try:
                code = runner.wait(timeout=self._settle)
            except subprocess.TimeoutExpired:
                return ModelPlaceOutcome(
                    started=True, detail="running past the settle window"
                )
            if code == 0:
                return ModelPlaceOutcome(started=True, detail="runner exited 0")
            return ModelPlaceOutcome(
                started=False, detail=f"runner exited {code}: {_last_log_line(log)}"
            )
        except (OSError, ValueError) as exc:
            return ModelPlaceOutcome(
                started=False,
                detail=f"dispatch failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}",
            )


def _last_line(text: str) -> str:
    """The last non-empty line of a command's output, the command's own reason for a refusal."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def _last_log_line(log: Path) -> str:
    try:
        lines = [
            line
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return "no log"
    return lines[-1][:200] if lines else "no output"


__all__ = [
    "FileBriefWriter",
    "FileDispatchRecord",
    "FileDriftOpenItems",
    "LedgerQueryLiveLanes",
    "SubprocessLaneRunner",
]
