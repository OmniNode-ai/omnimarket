"""Market-owned operator node smoke tests.

These are intentionally market-only. They prove the direct omnimarket entrypoints
still run without relying on external wrapper surfaces.
"""

from __future__ import annotations

import pytest

from omnimarket import market_skill_baseline
from omnimarket.market_skill_baseline import (
    ModelCommandResult,
    ModelContractInventory,
    ModelInputDrift,
    ModelMarketSkillResult,
    capture_market_skill_baseline,
    iter_market_skill_specs,
    render_markdown,
    run_cli_smoke,
)
from scripts import run_market_skill_baseline


def test_all_market_skill_cli_smokes_pass() -> None:
    for spec in iter_market_skill_specs():
        result = run_cli_smoke(spec)
        assert result.passed, (
            f"{spec.skill_name} smoke failed: rc={result.returncode} "
            f"summary={result.summary} stderr={result.stderr!r}"
        )


def test_market_skill_baseline_includes_ticket_pipeline() -> None:
    specs = {spec.skill_name: spec for spec in iter_market_skill_specs()}

    assert "ticket_pipeline" in specs
    assert specs["ticket_pipeline"].module == "omnimarket.nodes.node_ticket_pipeline"


def test_baseline_continues_after_one_skill_smoke_failure(monkeypatch) -> None:
    failing_skill = iter_market_skill_specs()[0].skill_name

    def fake_run_cli_smoke(spec):
        if spec.skill_name == failing_skill:
            raise RuntimeError("boom")
        return ModelCommandResult(
            passed=True,
            command=["python", "-m", spec.module],
            returncode=0,
            summary={"ok": True},
        )

    monkeypatch.setattr(market_skill_baseline, "run_cli_smoke", fake_run_cli_smoke)

    def matching_input_drift(_contract, _spec):
        return market_skill_baseline.ModelInputDrift(matches=True)

    monkeypatch.setattr(
        market_skill_baseline, "_compute_input_drift", matching_input_drift
    )

    report = capture_market_skill_baseline(run_pytest=False)

    assert len(report.skills) == len(iter_market_skill_specs())
    failed = next(item for item in report.skills if item.skill_name == failing_skill)
    assert failed.overall_status == "failing"
    assert failed.cli_smoke.summary["stage"] == "cli_smoke"


def test_baseline_report_sanitizes_local_paths() -> None:
    result = ModelCommandResult(
        passed=True,
        command=[
            market_skill_baseline.sys.executable,
            str(market_skill_baseline.REPO_ROOT / "tmp" / "work"),
        ],
        returncode=0,
        summary={},
    )
    sanitized = market_skill_baseline._sanitize_command(result.command)

    assert sanitized == ["python", "<omnimarket>/tmp/work"]


def test_baseline_report_sanitizes_home_paths() -> None:
    value = (
        f"WARNING: Task directory not found: {market_skill_baseline.Path.home()}"
        "/.claude/tasks/ticket-pipeline"
    )

    assert (
        market_skill_baseline._sanitize_report_value(value)
        == "WARNING: Task directory not found: <home>/.claude/tasks/ticket-pipeline"
    )


def test_markdown_reports_cli_smoke_status_not_overall(monkeypatch) -> None:
    spec = iter_market_skill_specs()[0]

    def drifted_input(_contract, _skill_spec):
        return market_skill_baseline.ModelInputDrift(
            matches=False,
            contract_only_fields=["missing"],
        )

    monkeypatch.setattr(market_skill_baseline, "_compute_input_drift", drifted_input)
    monkeypatch.setattr(
        market_skill_baseline,
        "run_cli_smoke",
        lambda skill_spec: ModelCommandResult(
            passed=True,
            command=["python", "-m", skill_spec.module],
            returncode=0,
            summary={"ok": True},
        ),
    )

    report = capture_market_skill_baseline(
        run_pytest=False, skill_names={spec.skill_name}
    )
    markdown = render_markdown(report)

    assert report.skills[0].overall_status == "degraded"
    assert "- CLI smoke status: `pass`" in markdown


def test_session_orchestrator_summary_redacts_run_variant_values() -> None:
    summary = market_skill_baseline._summarize_session_orchestrator(
        {
            "status": "complete",
            "session_id": "sess-20260429-1559",
            "dry_run": True,
            "dispatch_queue": [],
            "dispatch_receipts": ["{}"],
        }
    )

    assert summary["session_id"] == "sess-<redacted>"
    assert summary["dispatch_receipt_count"] == 1


def test_session_orchestrator_smoke_runs_phase2_and_phase3() -> None:
    result = market_skill_baseline.run_cli_smoke(
        next(
            spec
            for spec in market_skill_baseline.iter_market_skill_specs()
            if spec.skill_name == "session_orchestrator"
        )
    )

    assert result.passed
    assert result.summary["dispatch_queue_count"] == 2
    assert result.summary["dispatch_receipt_count"] == 2


def test_streaming_baseline_prints_each_skill_result(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = ModelMarketSkillResult(
        skill_name="ticket_pipeline",
        contract=ModelContractInventory(
            contract_name="ticket_pipeline",
            node_name="node_ticket_pipeline",
            node_type="compute",
            timeout_ms=600_000,
            terminal_event="onex.evt.omnimarket.ticket-pipeline-completed.v1",
            inputs=["ticket_id"],
        ),
        input_drift=ModelInputDrift(matches=True),
        cli_smoke=ModelCommandResult(
            passed=True,
            command=["python", "-m", "omnimarket.nodes.node_ticket_pipeline"],
            returncode=0,
            summary={"stop_reason": "not_implemented"},
        ),
        pytest=ModelCommandResult(
            passed=True,
            command=["python", "-m", "pytest"],
            returncode=0,
            summary={"targets": ["tests/test_codex_runtime_client.py"]},
            notes=["1 passed"],
        ),
        overall_status="working",
    )

    monkeypatch.setattr(
        run_market_skill_baseline,
        "iter_market_skill_baseline_results",
        lambda **_kwargs: iter([item]),
    )

    report = run_market_skill_baseline._capture_streaming(
        run_pytest=True,
        skill_names=None,
    )

    output = capsys.readouterr().out
    assert report.working_count == 1
    assert (
        "[market-skill] ticket_pipeline node=node_ticket_pipeline status=working"
        in output
    )
    assert "[market-skill] ticket_pipeline cli=pass rc=0" in output
    assert "[market-skill] ticket_pipeline runtime_proof=pass rc=0" in output
