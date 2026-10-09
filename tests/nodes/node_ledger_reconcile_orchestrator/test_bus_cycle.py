# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator as production wires it: the contract's bus, the contract gateway, the local adapters (OMN-20677).

Only the programs the adapters start (`gh`, the ledger writer) are replaced; the ledger,
the archive, the clone registry, the overlay and every hop between the nodes are real.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.models.ledger_reconcile import (
    ModelReconcileRequest,
    ModelReconcileResult,
)
from omnimarket.nodes.node_ledger_reconcile_effect.protocols import (
    local_ledger_reconcile_adapters as adapters,
)
from omnimarket.nodes.node_ledger_reconcile_orchestrator import __main__ as cli

NODE = Path(cli.__file__).parent
ROWS = [
    "2026-09-22T10:00:00Z | CLAIM | lane=landed-lane | ticket=OMN-1 | scope=land market#11",
    "2026-09-22T10:05:00Z | CLAIM | lane=gone-lane | ticket=OMN-1 | scope=land omnimarket#12",
    "2026-09-22T10:10:00Z | CLAIM | lane=unknown-lane | ticket=OMN-1 | scope=investigate issue",
]
OVERLAY = "github_org: Example-Org\nrepo_aliases:\n  market: omnimarket\nbranch_prefixes: [lane]\n"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    registry = tmp_path / "registry_root"
    (registry / "omnimarket" / ".git").mkdir(parents=True)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    ledger = ledger_dir / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# Ledger\n" + "\n".join(ROWS) + "\n")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(OVERLAY)
    appended = tmp_path / "appended.jsonl"
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import json, sys\n"
        f"open({str(appended)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    monkeypatch.setenv("OMNI_HOME", str(registry))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv(adapters.OVERLAY_ENV, str(overlay))
    monkeypatch.setenv(
        adapters.APPEND_COMMAND_ENV,
        f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))}",
    )
    monkeypatch.setattr(adapters.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        if cmd[0] not in ("gh", "git"):
            return subprocess.run(  # the ledger writer really runs
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        if cmd[0] == "gh":
            assert cmd[cmd.index("--repo") + 1] == "Example-Org/omnimarket"
            number = int(cmd[3])
            if "commits" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="2026-09-22T09:00:00Z\n", stderr=""
                )
            merged = number == 11
            payload = {
                "state": "MERGED" if merged else "OPEN",
                "mergedAt": "2026-09-22T11:00:00Z" if merged else None,
                "mergeCommit": {"oid": "b" * 40} if merged else None,
                "title": "fix(OMN-1): claimed work",
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such commit")

    monkeypatch.setattr(adapters, "_run", fake_run)
    return {"ledger": ledger, "appended": appended, "registry": registry}


def _dispatch(
    request: ModelReconcileRequest, tmp_path: Path
) -> tuple[RuntimeLocal, list[str]]:
    payload = tmp_path / "input.json"
    payload.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        NODE / "contract.yaml",
        input_path=payload,
        state_root=tmp_path / "state",
        timeout=30,
        backend_overrides={"event_bus": "inmemory"},
    )
    with patch.object(
        EventBusInmemory, "publish", autospec=True, side_effect=EventBusInmemory.publish
    ) as publish:
        runtime.run()
    topics = [
        call.args[1] if len(call.args) > 1 else call.kwargs["topic"]
        for call in publish.call_args_list
    ]
    return runtime, topics


def test_report_mode_travels_the_contract_bus_and_appends_nothing(
    host: dict[str, Path], tmp_path: Path
) -> None:
    request = ModelReconcileRequest(since_days=0)
    runtime, topics = _dispatch(request, tmp_path)
    assert topics == [
        "onex.cmd.omnimarket.ledger-reconcile-requested.v1",
        "onex.evt.omnimarket.ledger-reconciled.v1",
    ]
    assert runtime.exit_code == 0, runtime.last_error
    result = runtime.handler_result
    assert isinstance(result, ModelReconcileResult)
    assert result.correlation_id == request.correlation_id
    assert (result.exit_code, result.status, result.dangling) == (1, "report-only", 3)
    assert "COMPLETED  CLAIM landed-lane" in result.stdout
    assert "ORPHANED   CLAIM gone-lane" in result.stdout
    assert "UNKNOWN    CLAIM unknown-lane" in result.stdout
    assert not host["appended"].exists()
    evidence = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert evidence["wire_correlation_id"] == str(request.correlation_id)
    assert evidence["handler_locus"] == "in_process"
    assert runtime._events_received["(terminal)"] == 1
    assert runtime._handlers_wired == ["HandlerLedgerReconcileOrchestrator"]


def test_apply_mode_appends_the_planned_rows_through_the_ledger_writer(
    host: dict[str, Path], tmp_path: Path
) -> None:
    runtime, _ = _dispatch(
        ModelReconcileRequest(apply=True, max_appends=60, since_days=0), tmp_path
    )
    result = runtime.handler_result
    assert isinstance(result, ModelReconcileResult)
    assert (result.exit_code, result.status) == (1, "reconciled")
    assert (result.auto_closed, result.needs_attention, result.unknown) == (1, 1, 1)
    calls = [json.loads(line) for line in host["appended"].read_text().splitlines()]
    assert [(call[0], call[1]) for call in calls] == [
        (str(host["ledger"]), "--append")
    ] * 2
    assert "RECONCILER-AUTO-CLOSE" in calls[0][2]
    assert "lane=landed-lane" in calls[0][2]
    assert "RECONCILER-NEEDS-ATTENTION" in calls[1][2]


def test_a_host_without_its_ledger_is_not_run_not_clean(
    host: dict[str, Path], tmp_path: Path
) -> None:
    host["ledger"].unlink()
    runtime, _ = _dispatch(ModelReconcileRequest(), tmp_path)
    result = runtime.handler_result
    assert isinstance(result, ModelReconcileResult)
    assert (result.exit_code, result.status) == (3, "blocked")
    assert result.stderr.startswith("ledger_reconcile: NOT RUN — live ledger missing")


def test_the_cli_keeps_the_exits_and_prints_the_report(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--report", "--since-days", "0"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ledger_reconcile (OMN-17466) — REPORT mode\n")
    assert "DANGLING CLAIMS: 3" in out


def test_the_cli_workflow_flag_turns_a_reported_finding_into_exit_zero(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--report", "--since-days", "0", "--workflow", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["exit_code"] == 1
    assert printed["status"] == "report-only"


def test_the_cli_exits_three_when_the_host_cannot_reconcile(
    host: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_PATH")
    assert cli.main(["--report"]) == 3
    assert (
        "ONEX_LEDGER_PATH is not set (required, no default)" in capsys.readouterr().err
    )


def test_the_cli_reads_the_roster_options() -> None:
    request, as_json, workflow = cli.parse_request(
        [
            "--apply",
            "--live-lane",
            "b",
            "--live-lane",
            "a",
            "--live-roster",
            "--silent-hours",
            "72",
        ]
    )
    assert request.apply
    assert request.live_lanes == frozenset({"a", "b"})
    assert request.live_roster_known
    assert request.silent_hours == 72
    assert (as_json, workflow) == (False, False)
