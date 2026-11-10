# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory and real adapters of the host repair dispatch ports (OMN-23389).

The in-memory ones are the default and what the tests inject; the real ones run the argv the overlay declares and
keep the dedupe state in a file replaced atomically, so a restart between two alarms loses neither.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    ModelHostMeasurement,
    ModelRepairLaneSpec,
    ModelRepairState,
)

Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


class MeasurementError(RuntimeError):
    """The host's df or findmnt could not be read."""


class DispatchError(RuntimeError):
    """The repair lane was not started."""


def run_command(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


def fill(argv: Sequence[str], **values: str) -> list[str]:
    """The overlay's argv with ``$VAR`` read from the environment, then each ``{name}`` replaced; a value is one argument, never split."""
    out: list[str] = []
    for template in argv:
        arg = os.path.expandvars(template)
        for name, value in values.items():
            arg = arg.replace("{" + name + "}", value)
        out.append(arg)
    return out


class InMemoryStateStore:
    def __init__(self) -> None:
        self.rows: dict[str, ModelRepairState] = {}

    def get(self, key: str) -> ModelRepairState | None:
        return self.rows.get(key)

    def put(self, state: ModelRepairState) -> None:
        self.rows[state.key] = state

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


class JsonFileStateStore:
    """Durable state: one JSON document, replaced atomically on every write."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, ModelRepairState]:
        if not self.path.exists():
            return {}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        return {k: ModelRepairState.model_validate(v) for k, v in document.items()}

    def _write(self, rows: dict[str, ModelRepairState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".repair-state-"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({k: v.model_dump() for k, v in rows.items()}, indent=2)
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> ModelRepairState | None:
        return self._read().get(key)

    def put(self, state: ModelRepairState) -> None:
        rows = self._read()
        rows[state.key] = state
        self._write(rows)

    def delete(self, key: str) -> None:
        rows = self._read()
        if rows.pop(key, None) is not None:
            self._write(rows)


class InMemoryMeasurement:
    def __init__(self, readings: dict[str, ModelHostMeasurement] | None = None) -> None:
        self.readings = readings or {}
        self.calls: list[str] = []

    def measure(self, host: str) -> ModelHostMeasurement:
        self.calls.append(host)
        if host not in self.readings:
            raise MeasurementError(f"{host}: no reading")
        return self.readings[host]


class SshMeasurement:
    """Run the overlay's measure command for ``host``; its stdout is the df reading, the separator, the findmnt reading."""

    def __init__(
        self,
        argv: Sequence[str],
        separator: str,
        timeout: int,
        runner: Runner = run_command,
    ) -> None:
        self.argv, self.separator, self.timeout, self.runner = (
            argv,
            separator,
            timeout,
            runner,
        )

    def measure(self, host: str) -> ModelHostMeasurement:
        try:
            done = self.runner(fill(self.argv, host=host), self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MeasurementError(f"{host}: {type(exc).__name__}") from None
        if done.returncode != 0:
            raise MeasurementError(f"{host}: measure exit {done.returncode}")
        df, found, findmnt = done.stdout.partition(self.separator)
        if not found or not df.strip() or not findmnt.strip():
            raise MeasurementError(f"{host}: the reading holds no df or no findmnt")
        return ModelHostMeasurement(df=df.strip(), findmnt=findmnt.strip())


class InMemoryDispatcher:
    def __init__(self, refuse: bool = False) -> None:
        self.lanes: list[ModelRepairLaneSpec] = []
        self.refuse = refuse

    def dispatch(self, spec: ModelRepairLaneSpec) -> None:
        if self.refuse:
            raise DispatchError(f"{spec.lane}: refused")
        self.lanes.append(spec)


class ArgvDispatcher:
    def __init__(
        self, argv: Sequence[str], timeout: int, runner: Runner = run_command
    ) -> None:
        self.argv, self.timeout, self.runner = argv, timeout, runner

    def dispatch(self, spec: ModelRepairLaneSpec) -> None:
        Path(spec.brief_file).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.brief_file).write_text(spec.brief, encoding="utf-8")
        command = fill(
            self.argv,
            lane=spec.lane,
            brief_file=spec.brief_file,
            target=spec.target,
            ticket=spec.ticket,
            parent=spec.parent,
            ledger=spec.ledger,
        )
        try:
            done = self.runner(command, self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchError(f"{spec.lane}: {type(exc).__name__}") from None
        if done.returncode != 0:
            raise DispatchError(f"{spec.lane}: dispatch exit {done.returncode}")


class InMemoryEscalation:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def escalate(self, row: str) -> None:
        self.rows.append(row)


class ArgvEscalation:
    def __init__(
        self, argv: Sequence[str], timeout: int, runner: Runner = run_command
    ) -> None:
        self.argv, self.timeout, self.runner = argv, timeout, runner

    def escalate(self, row: str) -> None:
        done = self.runner(fill(self.argv, row=row), self.timeout)
        if done.returncode != 0:
            raise DispatchError(f"escalation row refused (exit {done.returncode})")
