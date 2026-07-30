# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15373: headless runner contract for the git-automation drift guard.

The guard only has force if the scheduled job's exit status tracks the verdict.
These tests pin that contract: exit 1 on drift, exit 0 on clean, exit 2 on a
misconfigured invocation — and prove the shipped exceptions registry is loadable
and empty, so nothing is being silently suppressed on day one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_linear_triage import guard_cli
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    EnumGitAutomationVerdict as V,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationAuditReport,
    ModelGitAutomationFinding,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY = _REPO_ROOT / "config/linear_git_automation_exceptions.yaml"


class _StubHandler:
    def __init__(self, report: ModelGitAutomationAuditReport) -> None:
        self._report = report

    def handle(self, **_: Any) -> ModelGitAutomationAuditReport:
        return self._report


def _install(
    monkeypatch: pytest.MonkeyPatch, report: ModelGitAutomationAuditReport
) -> None:
    monkeypatch.setattr(
        guard_cli, "HandlerGitAutomationGuard", lambda: _StubHandler(report)
    )


def _drift_report() -> ModelGitAutomationAuditReport:
    return ModelGitAutomationAuditReport(
        passed=False,
        findings=[
            ModelGitAutomationFinding(
                team_key="CON",
                automation_id="a1",
                event="merge",
                state_name="Done",
                state_type="completed",
                verdict=V.DRIFT,
                reason="mints Done with zero proof",
                all_branches=True,
            )
        ],
        drift_count=1,
        failure_reason="1 git automation(s) resolve to a completed-type state",
    )


def _clean_report() -> ModelGitAutomationAuditReport:
    return ModelGitAutomationAuditReport(
        passed=True,
        findings=[
            ModelGitAutomationFinding(
                team_key="OMN",
                automation_id="a1",
                event="merge",
                state_name="In Review",
                state_type="started",
                verdict=V.CLEAN,
                reason="not a completed-type state",
                all_branches=True,
            )
        ],
        clean_count=1,
    )


def test_exit_1_on_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _drift_report())
    assert guard_cli.main([]) == 1


def test_exit_0_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _clean_report())
    assert guard_cli.main([]) == 0


def test_drift_failure_names_the_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A red run must say what to do, not just that something is wrong."""
    _install(monkeypatch, _drift_report())
    guard_cli.main([])
    err = capsys.readouterr().err
    assert "dod_verify" in err
    assert "gitAutomationStateUpdate" in err


def test_report_json_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _drift_report())
    out = tmp_path / "report.json"
    assert guard_cli.main(["--report-json", str(out)]) == 1
    assert '"passed": false' in out.read_text(encoding="utf-8")


def test_missing_exceptions_file_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A registry path that does not resolve is a configuration error, not a
    silent 'no exceptions' — that tolerance is how a typo becomes a bypass."""
    _install(monkeypatch, _clean_report())
    assert guard_cli.main(["--exceptions", str(tmp_path / "nope.yaml")]) == 2


def test_malformed_exception_entry_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An entry missing the mandatory expiry is refused, never treated as
    permanent."""
    _install(monkeypatch, _clean_report())
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "exceptions:\n  - automation_id: a1\n    team_key: CON\n"
        "    owner: someone\n    reason: because\n",
        encoding="utf-8",
    )
    assert guard_cli.main(["--exceptions", str(bad)]) == 2


def test_shipped_registry_loads_and_is_empty() -> None:
    """The registry that ships with this PR suppresses nothing.

    Two teams (CON, JON) are drifted as of the 2026-07-30 readback and are
    recorded in OMN-15373 as pending an owner decision. Pre-registering a
    suppression on their behalf would ship the guard green over a workspace
    that is still forgeable.
    """
    assert guard_cli.load_exceptions(_REGISTRY) == []


def test_a_registry_entry_round_trips() -> None:
    """The registry format actually parses when populated (not a dead schema)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "exc.yaml"
        p.write_text(
            "exceptions:\n"
            "  - automation_id: 2ee841f5-f42d-408c-a00e-ddce2c763b58\n"
            "    team_key: CON\n"
            "    owner: Daniyal\n"
            "    reason: contractor lane, decision pending\n"
            "    expires_at: 2026-08-30T00:00:00Z\n",
            encoding="utf-8",
        )
        loaded = guard_cli.load_exceptions(p)
    assert len(loaded) == 1
    assert loaded[0].owner == "Daniyal"
    assert loaded[0].expires_at > datetime.now(UTC) - timedelta(days=3650)
