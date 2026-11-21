# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime backend: the ledger read and appended over the bus, the plan owner check run as its node."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelLedgerSettings,
    ModelPlanOwnerSettings,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.protocols import (
    LedgerUnreadableError,
    PlanOwnerUnreadableError,
    RowAppendFailedError,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_MISSING = re.compile(r"^MISSING (\S+)", re.MULTILINE)
Run = Callable[..., "subprocess.CompletedProcess[str]"]


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


class LedgerQueryRows:
    """The rows of the lookback window, one ``onex ledger query --query rows`` per kind, rebuilt as row lines."""

    def __init__(
        self,
        settings: ModelLedgerSettings,
        clock: Callable[[], datetime],
        run: Run = subprocess.run,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._run = run

    def _kind(self, kind: str, since: str) -> list[str]:
        settings = self._settings
        argv = [
            *settings.query_command,
            "--query",
            "rows",
            "--kind",
            kind,
            "--since",
            since,
            "--limit",
            str(settings.row_limit),
        ]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=settings.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LedgerUnreadableError(
                f"ledger query {kind}: {type(exc).__name__}"
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
            raise LedgerUnreadableError(
                f"ledger query {kind} exited {done.returncode}: {_last_line(done.stderr or done.stdout or '')}"
            )
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise LedgerUnreadableError(
                f"ledger query {kind} answered no JSON"
            ) from exc
        if answer.get("error") or answer.get("refusal"):
            raise LedgerUnreadableError(
                f"ledger query {kind} refused: {answer.get('error') or answer.get('refusal')}"
            )
        rows = answer.get("rows") or []
        matched = answer.get("matched")
        truncated = (
            matched > len(rows)
            if isinstance(matched, int)
            else len(rows) >= settings.row_limit
        )
        if truncated:
            raise LedgerUnreadableError(
                f"ledger query {kind} hit the row limit {settings.row_limit}: the window is not read whole"
            )
        lines: list[str] = []
        for row in rows:
            raw = str(row.get("raw_row") or row.get("text") or "")
            if raw.startswith(("20", "19")) and " | " in raw:
                lines.append(raw)
        return lines

    def read(self) -> tuple[str, ...]:
        since = (
            (self._clock() - timedelta(hours=self._settings.lookback_hours))
            .astimezone(UTC)
            .strftime(_STAMP)
        )
        lines: list[str] = []
        for kind in self._settings.kinds:
            lines.extend(self._kind(kind, since))
        return tuple(lines)


class CommandPlanOwnerMissing:
    """Runs the plan owner check node; exit 0 or 1 is an answer (its MISSING lines), anything else is unreadable."""

    def __init__(
        self, settings: ModelPlanOwnerSettings, run: Run = subprocess.run
    ) -> None:
        self._settings = settings
        self._run = run

    def read(self) -> tuple[str, ...]:
        if not self._settings.command:
            return ()
        try:
            done = self._run(
                list(self._settings.command),
                capture_output=True,
                text=True,
                timeout=self._settings.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PlanOwnerUnreadableError(
                f"plan owner check: {type(exc).__name__}"
            ) from exc
        if done.returncode not in (0, 1):
            raise PlanOwnerUnreadableError(
                f"plan owner check exited {done.returncode}: {_last_line(done.stdout or done.stderr or '')}"
            )
        return tuple(_MISSING.findall(done.stdout))


class CommandRowAppender:
    """Appends one row through the ledger append command (``onex-ledger <ledger> --append``)."""

    def __init__(
        self, settings: ModelLedgerSettings, run: Run = subprocess.run
    ) -> None:
        self._settings = settings
        self._run = run

    def append(self, row: str) -> None:
        try:
            done = self._run(
                [*self._settings.append_command, row],
                capture_output=True,
                text=True,
                timeout=self._settings.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RowAppendFailedError(f"append: {type(exc).__name__}") from exc
        if done.returncode != 0:
            raise RowAppendFailedError(
                f"append exited {done.returncode}: {_last_line(done.stderr or done.stdout or '')}"
            )


__all__ = ["CommandPlanOwnerMissing", "CommandRowAppender", "LedgerQueryRows"]
