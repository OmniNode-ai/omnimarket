"""Market-owned operator node smoke tests.

These are intentionally market-only. They prove the direct omnimarket entrypoints
still run without relying on external wrapper surfaces.
"""

from __future__ import annotations

from omnimarket import market_skill_baseline
from omnimarket.market_skill_baseline import (
    ModelCommandResult,
    capture_market_skill_baseline,
    iter_market_skill_specs,
    render_markdown,
    run_cli_smoke,
)


def test_all_market_skill_cli_smokes_pass() -> None:
    for spec in iter_market_skill_specs():
        result = run_cli_smoke(spec)
        assert result.passed, (
            f"{spec.skill_name} smoke failed: rc={result.returncode} "
            f"summary={result.summary} stderr={result.stderr!r}"
        )


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
