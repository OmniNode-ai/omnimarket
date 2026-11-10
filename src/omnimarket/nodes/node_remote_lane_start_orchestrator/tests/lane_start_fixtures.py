# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures for the lane start tests: settings, a pool, a ledger fixture file and the ordered call log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)

from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
    ScriptedLoginDraw,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartRequest,
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    InMemoryReceiptSink,
    RemoteLaneStartOrchestrator,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)

PRINCIPAL = "lane-fixture-principal"
T0 = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
DONE_MESSAGE = (
    "Opened omnimarket#1 and handed it off.\n"
    "LANE_RESULT outcome=handed-off\nDELEGATION delegated=0 reason=no-text-or-code"
)


def settings(**overrides: Any) -> ModelRemoteLaneStartSettings:
    values: dict[str, Any] = {
        "supported_engines": ["claude"],
        "supported_placements": ["lab"],
        "engine_argv": ["claude", "-p", "{brief}", "--model", "{model}"],
        "engine_environment": {"LANE_MODE": "headless"},
        "start_timeout_seconds": 5,
        "prepare_timeout_seconds": 5,
        "host_call_timeout_seconds": 5,
        "poll_interval_seconds": 0,
        "stop_grace_seconds": 1,
        "receipt_dir": "/unused",
        "host_run_root": "/runs",
        "registry_root_candidates": ["/registry"],
        "registry_marker": "example_marker_dir",
        "ssh_argv": ["ssh"],
        "detach_argv": ["setsid"],
        "host_path_prefix": "",
        "login_pool_module": "example.token_pool_node",
        "login_pool_python_relative": ".example-venv/bin/python",
        "login_draw_timeout_seconds": 5,
        "ledger_append_argv": ["fixture-ledger"],
        "ledger_write_via": "bus",
        "ledger_writer_principal": PRINCIPAL,
        "actor": "script:onex-remote-lane",
        "displaces": "a lane on the busy launching host",
        "est_hours": 2,
        "priority": "other",
        "claim_text": "remote lane on {host} via node_remote_lane_start_orchestrator",
    }
    return ModelRemoteLaneStartSettings.model_validate(values | overrides)


def snapshot(
    *, slots: int = 2, local: bool = False, name: str = "h106"
) -> ModelLaneHostPoolSnapshot:
    return ModelLaneHostPoolSnapshot(
        readings=(
            ModelLaneHostPoolReading(
                name=name,
                target=name,
                local=local,
                engines=("claude_opus", "claude_sonnet"),
                lane_slots=slots,
                lane_cap=4,
                rank_free=4.0,
                mem_avail_gb=16.0,
            ),
        )
    )


def request(**overrides: Any) -> ModelRemoteLaneStartRequest:
    values: dict[str, Any] = {
        "lane": "floor-alpha-a1",
        "key": "floor:alpha:alpha-t1",
        "ticket": "OMN-22848",
        "parent": "orchestrator-fixture",
        "engine": "claude",
        "placement": "lab",
        "model": "sonnet",
        "ledger_path": "/fixture/ledger.md",
        "brief": "print the host name and stop",
        "run_id": "rlane-floor-alpha-a1-000001",
        "attempt": 1,
    }
    return ModelRemoteLaneStartRequest.model_validate(values | overrides)


class FixtureLedgerExecutor:
    """Append rows to a ledger fixture file, as the ledger writer would; log each append in ``calls``."""

    def __init__(self, path: Path, calls: list[str], refuse: bool = False) -> None:
        self._path = path
        self._calls = calls
        self._refuse = refuse
        self.attempts: list[
            str
        ] = []  # every row handed to the writer, a refused one too

    def append(self, rows: str) -> ModelAppendCommandResult:
        kind = rows.split(" | ")[1]
        self.attempts.append(rows.rstrip("\n"))
        if self._refuse:
            self._calls.append(f"ledger:refused {kind}")
            return ModelAppendCommandResult(
                exit_code=65, stderr="row refused by the grammar"
            )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(rows.rstrip("\n") + "\n")
        self._calls.append(f"ledger:append {kind}")
        return ModelAppendCommandResult(exit_code=0, stdout="appended")


def ledger_effect(
    path: Path,
    calls: list[str],
    refuse: bool = False,
    executor: FixtureLedgerExecutor | None = None,
) -> HandlerWorkLedgerAppendEffect:
    if not path.exists():
        path.write_text("# fixture ledger\n", encoding="utf-8")
    return HandlerWorkLedgerAppendEffect(
        runner=executor or FixtureLedgerExecutor(path, calls, refuse),
        reader=LocalLedgerFile(path),
        writer_principals=[PRINCIPAL],
    )


def stream_result(message: str) -> str:
    return json.dumps({"type": "result", "result": message})


class Rig:
    """One orchestrator over the scripted transport and a ledger fixture file, sharing one call log."""

    def __init__(
        self,
        tmp: Path,
        script: ModelScriptedTransportScript | None = None,
        *,
        pool: ModelLaneHostPoolSnapshot | None = None,
        refuse_ledger: bool = False,
        stop_after_polls: int | None = None,
        cfg: ModelRemoteLaneStartSettings | None = None,
        calls: list[str] | None = None,
        now: datetime = T0,
        login_labels: tuple[str | None, ...] | None = None,
        placement: HandlerRemoteLanePlacement | None = None,
    ) -> None:
        self.calls: list[str] = calls if calls is not None else []
        self.ledger_path = tmp / "ledger.md"
        self.transport = ScriptedLaneHostTransport(
            script or ModelScriptedTransportScript(), self.calls
        )
        self.receipts = InMemoryReceiptSink()
        self.printed: list[str] = []
        self._polls = 0
        self._stop_after = stop_after_polls

        def stop_requested() -> bool:
            if self._stop_after is None:
                return False
            self._polls += 1
            return self._polls > self._stop_after

        self.executor = FixtureLedgerExecutor(
            self.ledger_path, self.calls, refuse_ledger
        )
        self.orchestrator = RemoteLaneStartOrchestrator(
            settings=cfg or settings(),
            host_pool=InMemoryLaneHostPool(pool or snapshot()),
            ledger=ledger_effect(
                self.ledger_path, self.calls, refuse_ledger, self.executor
            ),
            ledger_reader=LocalLedgerFile(self.ledger_path),
            host=HandlerRemoteLaneStartEffect(self.transport),
            login=None
            if login_labels is None
            else ScriptedLoginDraw(login_labels, self.calls),
            placement=placement,
            receipts=self.receipts,
            echo=self.printed.append,
            clock=lambda: now,
            stop_requested=stop_requested,
        )

    def ledger_rows(self, kind: str | None = None) -> list[str]:
        text = self.ledger_path.read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if " | " in line]
        return [r for r in rows if kind is None or r.split(" | ")[1] == kind]

    def attempted_rows(self, kind: str) -> list[str]:
        """Rows of ``kind`` handed to the ledger writer, a refused one included."""
        return [r for r in self.executor.attempts if r.split(" | ")[1] == kind]

    def host_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("host:")]


def cells(row: str) -> dict[str, str]:
    """A row's key=value cells, time and run id excluded."""
    out: dict[str, str] = {}
    for cell in row.split(" | ")[2:]:
        name, eq, value = cell.partition("=")
        if eq and " " not in name and name not in ("run", "req"):
            out[name] = value
    return out
