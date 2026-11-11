# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the ledger route tests (OMN-23691): the bus read command as a fake, the environment and the rows."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    RULES_EXAMPLE,
    T0,
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_effect/contract.yaml"
)
#: The variable the effect's contract names as the ledger file's path: the old route the bus read command replaces.
FILE_ROUTE_ENV: str = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime"][
    "ledger"
]["path_env"]
COMMAND_ENV = "ONEX_LEDGER_BUS_READ_COMMAND"
ROUTE_DOCUMENT: dict[str, Any] = {
    "route": "command",
    "command_env": COMMAND_ENV,
    "timeout_seconds": 20,
    "row_limit": 500,
}
ROUTE = ModelCommandLedgerReadRoute.model_validate(ROUTE_DOCUMENT)


def ledger_line(kind: str, lane: str, ticket: str, minutes_before: int = 60) -> str:
    stamp = (T0 - timedelta(minutes=minutes_before)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | {kind} | lane={lane} | ticket={ticket} | model=m"


class FakeBusReadCommand:
    """The bus read command's stand-in: answers the typed query JSON it is sent, the way the work ledger query node answers it.

    ``exit_code`` makes it refuse, ``matched`` makes the answer claim more rows than it holds (a truncated answer) and
    ``row_key`` renames the key that carries each row's text (the injected break of the seam).
    """

    def __init__(
        self,
        lines: list[str],
        *,
        exit_code: int = 0,
        matched: int | None = None,
        row_key: str = "raw_row",
    ) -> None:
        self.lines = lines
        self.exit_code = exit_code
        self.matched = matched
        self.row_key = row_key
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        request = json.loads(kwargs["input"])
        self.requests.append(request)
        if self.exit_code:
            return subprocess.CompletedProcess(
                args,
                self.exit_code,
                "",
                "Work-ledger read refused (WorkLedgerQueryError)",
            )
        rows = [
            {self.row_key: line, "projected_at": "2026-11-10T02:30:00Z"}
            for line in self.lines
            if line.split(" | ")[1] in request["kinds"]
        ]
        body: dict[str, Any] = {"status": "ok", "rows": rows}
        if self.matched is not None:
            body["matched"] = self.matched
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def install_fake(
    monkeypatch: pytest.MonkeyPatch, fake: FakeBusReadCommand
) -> FakeBusReadCommand:
    """Put the fake where the command row source runs its command."""
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def environment(
    tmp_path: Path, *, with_command: bool = True, ledger_file: str | None = None
) -> dict[str, str]:
    """The projection's environment: tmp_path as home, the rules overlay copied beside it, no ledger file unless one is named."""
    overlay = tmp_path / "work-state-rules.yaml"
    shutil.copyfile(RULES_EXAMPLE, overlay)
    env = {"OMNI_HOME": str(tmp_path), "ONEX_WORK_STATE_OVERLAY": str(overlay)}
    if with_command:
        env[COMMAND_ENV] = "fake-read --stdin"
    if ledger_file is not None:
        env[FILE_ROUTE_ENV] = ledger_file
    return env


def ledger_file(tmp_path: Path, *lines: str) -> str:
    path = tmp_path / "ledger" / "ledger-fixture.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def snapshots_text(tmp_path: Path) -> str:
    """Every stored source snapshot, as one string."""
    root = tmp_path / ".onex_state" / "work-state" / "snapshots"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(root.glob("*.json")))


def unread_for(unread: Iterable[str], source: str) -> list[str]:
    return [u for u in unread if u.startswith(f"{source}:")]
