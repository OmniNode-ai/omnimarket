import json
import subprocess
import sys
from pathlib import Path

import yaml


def _ticket_pipeline_terminal_event() -> str:
    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_ticket_pipeline"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text())
    return str(raw["terminal_event"])


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_ticket_pipeline",
            "OMN-9530",
            "--dry-run",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_text() -> None:
    res = _run("--output", "text")

    assert res.returncode == 0
    assert "OMNIMARKET SKILL: ticket_pipeline" in res.stdout
    assert "INFO" not in res.stdout.splitlines()[0]


def test_dry_run_json() -> None:
    res = _run("--output", "json")

    assert res.returncode == 0
    parsed = json.loads(res.stdout)
    assert parsed["skill_name"] == "ticket_pipeline"
    assert parsed["status"] in {"blocked", "partial"}
    assert set(parsed) == {
        "skill_name",
        "node_name",
        "contract_name",
        "contract_version",
        "run_id",
        "correlation_id",
        "mode",
        "status",
        "input_summary",
        "steps",
        "evidence",
        "result_summary",
        "terminal_event",
        "output_config",
        "started_at",
        "completed_at",
        "duration_ms",
    }
    assert parsed["result_summary"]["stopped_at"] == "blocked"
    assert parsed["result_summary"]["stop_reason"] == "not_implemented"
    assert any(
        item["name"] == "implement" and item["status"] == "succeeded"
        for item in parsed["steps"]
    )
    assert parsed["terminal_event"] == _ticket_pipeline_terminal_event()


def test_dry_run_yaml() -> None:
    res = _run("--output", "yaml")

    assert res.returncode == 0
    parsed = yaml.safe_load(res.stdout)
    assert parsed["skill_name"] == "ticket_pipeline"


def test_dry_run_markdown() -> None:
    res = _run("--output", "markdown")

    assert res.returncode == 0
    assert "## Execution" in res.stdout
    assert "## Result" in res.stdout
