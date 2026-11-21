# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler of the machine principal grant audit node (OMN-21622)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_principal_grant_audit_effect.handlers.handler_principal_grant_audit import (
    OVERLAY_ENV,
    HandlerPrincipalGrantAudit,
    PrincipalGrantAuditConfigurationError,
)
from omnimarket.nodes.node_principal_grant_audit_effect.models import (
    ModelPrincipalGrantAuditRequest,
)

pytestmark = pytest.mark.unit


class FakeRunner:
    """Record every argv and answer the inspect and audit calls with configurable results."""

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
    return HandlerPrincipalGrantAudit(run=runner.run, python="python")


def test_golden_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "ok"
    assert result.exit_code == 0
    assert len(result.principals) == 1
    assert result.principals[0].principal == "m1"
    assert result.principals[0].expected == 3
    assert result.principals[0].live == 3
    assert result.principals[0].missing == 0
    assert result.principals[0].extra == 0


def test_golden_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_exit = 1
    runner.audit_stdout = (
        "principal=m1 expected=3 live=2 missing=1 extra=0\n"
        "MISSING m1 topic LITERAL some.topic read\n"
    )
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "drift"
    assert result.exit_code == 1
    assert "MISSING m1 topic LITERAL some.topic read" in result.missing_lines


def test_exact_audit_and_inspect_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    _handler(runner).handle(ModelPrincipalGrantAuditRequest(registry_root=tmp_path))
    inspect_argv = next(
        c for c in runner.calls if c[0] == "docker" and c[1] == "inspect"
    )
    assert inspect_argv == [
        "docker",
        "inspect",
        "--format",
        '{{range .Mounts}}{{if eq .Destination "/overlay/doc.yaml"}}{{.Source}}{{end}}{{end}}',
        "container-a",
    ]
    audit_argv = next(c for c in runner.calls if "audit-grants" in c)
    assert audit_argv == [
        "python",
        str(tmp_path / "infra" / "scripts" / "audit.py"),
        "audit-grants",
        "--grants",
        str(tmp_path / "infra" / "grants.yaml"),
        "--ledger",
        str(tmp_path / "ledger.jsonl"),
        "--overlay",
        str(tmp_path / "issuer-overlay.yaml"),
        "--role",
        "machine",
        "--rpk-container",
        "container-a",
    ]


def test_container_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.inspect_exit = 1
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "container_unreadable"
    assert result.exit_code == 2
    assert not any("audit-grants" in c for c in runner.calls)


def test_blank_inspect_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.inspect_stdout = ""
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "container_unreadable"
    assert result.exit_code == 2
    assert not any("audit-grants" in c for c in runner.calls)


def test_audit_error_carries_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_exit = 2
    runner.audit_stderr = "boom"
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "audit_error"
    assert result.exit_code == 2
    assert any("boom" in e for e in result.errors)


@pytest.mark.parametrize("code", [124, 127])
def test_audit_timeout_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_exit = code
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "audit_error"
    assert result.exit_code == 2


def test_empty_audit_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_stdout = ""
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "audit_error"
    assert result.exit_code == 2


def test_dry_run_records_audit_argv_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path, dry_run=True)
    )
    assert result.status == "dry_run"
    assert result.exit_code == 0
    assert any("audit-grants" in c.argv for c in result.steps)
    assert not any("audit-grants" in c for c in runner.calls)


def test_extra_lines_only_is_ok_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(_overlay(tmp_path)))
    runner = FakeRunner(tmp_path)
    runner.audit_stdout = (
        "principal=m1 expected=3 live=3 missing=0 extra=1\n"
        "EXTRA m1 topic LITERAL some.topic write\n"
    )
    result = _handler(runner).handle(
        ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
    )
    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.principals[0].extra == 1


def test_no_overlay_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(PrincipalGrantAuditConfigurationError):
        _handler(FakeRunner(tmp_path)).handle(
            ModelPrincipalGrantAuditRequest(registry_root=tmp_path)
        )
