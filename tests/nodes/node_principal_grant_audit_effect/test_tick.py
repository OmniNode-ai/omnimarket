# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the machine principal grant audit node (OMN-21622)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_principal_grant_audit_effect.handlers.handler_principal_grant_audit import (
    OVERLAY_ENV,
    HandlerPrincipalGrantAudit,
)
from omnimarket.nodes.node_principal_grant_audit_effect.runtime_principal_grant_audit_tick import (
    main,
    run_once,
)

pytestmark = pytest.mark.unit


class FakeRunner:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.calls: list[list[str]] = []
        self.inspect_exit = 0
        self.inspect_stdout = f"{tmp}/issuer-overlay.yaml\n"
        self.audit_exit = 0
        self.audit_stdout = "principal=m1 expected=3 live=3 missing=0 extra=0\n"
        self.audit_stderr = ""

    def run(self, argv: Any, **_: Any) -> Any:
        import subprocess

        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "docker" and argv[1] == "inspect":
            return subprocess.CompletedProcess(
                argv, self.inspect_exit, self.inspect_stdout, ""
            )
        if "audit-grants" in argv:
            return subprocess.CompletedProcess(
                argv, self.audit_exit, self.audit_stdout, self.audit_stderr
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


def _overlay(tmp: Path) -> Path:
    overlay = tmp / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "infra_checkout": "infra",
                "script": "scripts/audit.py",
                "grants_file": "grants.yaml",
                "issuer_container": "container-a",
                "overlay_mount_target": "/overlay/doc.yaml",
                "ledger_file": "ledger.jsonl",
                "role": "machine",
                "timeout_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    return overlay


def _handler(runner: FakeRunner) -> HandlerPrincipalGrantAudit:
    return HandlerPrincipalGrantAudit(run=runner.run)


def test_probe_loads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)

    def _refuse(run: Any) -> HandlerPrincipalGrantAudit:
        raise AssertionError("the probe must not load an overlay or run a handler")

    code = main(["--probe"], handler=_refuse)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "probe_ok"


def test_run_once_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    code, line = run_once(
        registry_root=tmp_path, dry_run=False, handler=_handler(runner)
    )
    assert code == 0
    assert line["status"] == "ok"
    assert line["principals"] == 1


def test_run_once_drift_prints_missing_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_exit = 1
    runner.audit_stdout = (
        "principal=m1 expected=3 live=2 missing=1 extra=0\n"
        "MISSING m1 topic LITERAL some.topic read\n"
    )
    code = main(["--omni-home", str(tmp_path)], handler=_handler(runner))
    assert code == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "MISSING m1 topic LITERAL some.topic read"
    assert json.loads(out[-1])["status"] == "drift"


def test_heartbeat_file_holds_exit_code_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    state_dir = tmp_path / "state"
    code = main(
        ["--omni-home", str(tmp_path), "--state-dir", str(state_dir)],
        handler=_handler(runner),
    )
    record = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert code == 0
    assert record["exit_code"] == 0
    assert record["result"]["status"] == "ok"


def test_overlay_refused_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, "")
    state_dir = tmp_path / "state"
    code = main(
        ["--omni-home", str(tmp_path), "--state-dir", str(state_dir)],
    )
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "overlay_refused"
    assert (state_dir / "last-run.json").exists()


def test_missing_registry_root_without_probe_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(tmp_path / "state")])
    assert exc.value.code == 2
