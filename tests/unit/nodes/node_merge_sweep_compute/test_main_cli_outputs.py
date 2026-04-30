import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_merge_sweep_compute.__main__ import (
    _required_check_state,
    main,
)


class _FakeGitHub:
    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return [
            {
                "number": 462,
                "title": "feat(OMN-10316): wire docs-validate",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "reviewDecision": None,
                "statusCheckRollup": [],
                "labels": [],
                "headRefOid": "abc123",
            }
        ]

    def fetch_branch_protection(self, repo: str) -> int | None:
        return None


def _terminal_event() -> str:
    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_merge_sweep_compute"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text())
    return str(raw["terminal_event"])


def _run(capsys, *extra: str) -> tuple[int, str]:
    rc = main(
        [
            "--repos",
            "OmniNode-ai/omnimarket",
            "--dry-run",
            "--no-require-approval",
            *extra,
        ],
        github=_FakeGitHub(),
    )
    captured = capsys.readouterr()
    return rc, captured.out


def test_dry_run_text(capsys) -> None:
    rc, out = _run(capsys, "--output", "text")

    assert rc == 0
    assert "OMNIMARKET SKILL: merge_sweep" in out
    assert "OmniNode-ai/omnimarket#462" in out
    assert "Merge-ready" in out


def test_dry_run_json(capsys) -> None:
    rc, out = _run(capsys, "--output", "json")

    assert rc == 0
    parsed = json.loads(out)
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
    assert parsed["skill_name"] == "merge_sweep"
    assert parsed["node_name"] == "node_merge_sweep_compute"
    assert parsed["terminal_event"] == _terminal_event()
    assert parsed["result_summary"]["merge_sweep_status"] == "queued"
    assert parsed["result_summary"]["track_counts"]["A"] == 1
    assert parsed["steps"][0]["name"] == "OmniNode-ai/omnimarket#462"
    assert parsed["steps"][0]["status"] == "A"


def test_dry_run_yaml(capsys) -> None:
    rc, out = _run(capsys, "--output", "yaml")

    assert rc == 0
    parsed = yaml.safe_load(out)
    assert parsed["skill_name"] == "merge_sweep"
    assert parsed["result_summary"]["track_counts"]["A"] == 1


def test_dry_run_markdown(capsys) -> None:
    rc, out = _run(capsys, "--output", "markdown")

    assert rc == 0
    assert "# Skill: merge_sweep" in out
    assert "## Execution" in out
    assert "OmniNode-ai/omnimarket#462" in out


def test_required_check_state_distinguishes_pending_from_failed() -> None:
    assert _required_check_state(
        {
            "statusCheckRollup": [
                {"isRequired": True, "status": "IN_PROGRESS", "conclusion": None}
            ]
        }
    ) == (False, False, True)
    assert _required_check_state(
        {"statusCheckRollup": [{"isRequired": True, "conclusion": "FAILURE"}]}
    ) == (False, True, False)
