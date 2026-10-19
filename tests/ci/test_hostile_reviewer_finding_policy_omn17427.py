# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-reviewer workflow runs the finding policy before it reads the verdict (OMN-17427).

The veto shell block of the workflow is EXTRACTED and EXECUTED against a scratch git
repository whose base commit carries the node, as the gate runs it:

* a blocking finding on a line the pull request did not change is dropped and the
  verdict read afterwards is ``passed``; one on a line it changed stays blocking;
* the round state of the previous run is read from the file the earlier step writes,
  and the next run's state, the findings that block and those dropped are reported;
* a base commit whose node predates the policy still runs the parse veto, with no
  policy arguments;
* only a comment the Actions bot wrote can carry the round state.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "hostile-reviewer.yml"
NODE = "node_review_finding_veto_effect"
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE
POLICY_HANDLER = "handlers/handler_review_finding_policy.py"


OLD_MAIN = """\
import argparse
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--result-file", required=True)
parser.add_argument("--head-sha", required=True)
parser.add_argument("--repo-root", default=".")
parser.parse_args()
sys.stderr.write("old veto ran" + chr(10))
"""


def _veto_block() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index('          VETO_BASE="')
    end = text.index("          # Parse verdict from JSON using Python inline.", start)
    return "\n".join(line[10:] for line in text[start:end].splitlines())


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *argv],
        check=True, capture_output=True, text=True, timeout=60,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()  # fmt: skip


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _cluster(line: int, title: str) -> dict[str, Any]:
    return {
        "file_path": "app.py", "line_start": line, "severity": "error",
        "rule": "category:logic_error", "message": f"{title} | detail",
        "agreeing_models": ["a", "b"], "agreement_count": 2, "blocking": True,
        "finding_ids": ["a1", "b1"],
    }  # fmt: skip


def _result(*clusters: dict[str, Any]) -> dict[str, Any]:
    return {
        "models_succeeded": ["a", "b"],
        "results": [],
        "quorum": {
            "verdict": "blocked", "blocking_count": len(clusters), "warning_count": 0,
            "blocking_findings": list(clusters), "warning_findings": [],
        },
    }  # fmt: skip


def _run_block(
    tmp_path: Path,
    document: dict[str, Any],
    *,
    base_has_policy: bool = True,
    prior_state: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    node_target = work / "src" / "omnimarket" / "nodes" / NODE
    shutil.copytree(NODE_DIR, node_target, ignore=shutil.ignore_patterns("__pycache__"))
    if not base_has_policy:
        # The node as the veto-only revision shipped it: it rejects any argument but these.
        (node_target / POLICY_HANDLER).unlink()
        (node_target / "__main__.py").write_text(OLD_MAIN, encoding="utf-8")
    (work / "src" / "omnimarket" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "omnimarket" / "nodes" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (work / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    base = _commit(work, "base")
    (work / "app.py").write_text("one\ntwo\nthree changed\n", encoding="utf-8")
    head = _commit(work, "head")

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(document), encoding="utf-8")
    if prior_state is not None:
        (tmp_path / "hostile-review-prior-state.json").write_text(
            prior_state, encoding="utf-8"
        )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    # The step runs `uv run --no-sync python -m ...`; the shim drops `run --no-sync python`.
    uv.write_text(
        f'#!/bin/sh\nshift 3\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    completed = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _veto_block()],
        capture_output=True, text=True, timeout=120, check=True,
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GITHUB_WORKSPACE": str(work),
            "RUNNER_TEMP": str(tmp_path),
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "REVIEW_JSON_FILE": str(result_file),
        },
    )  # fmt: skip
    report_file = tmp_path / "hostile-review-report.json"
    report = (
        json.loads(report_file.read_text(encoding="utf-8"))
        if report_file.exists()
        else None
    )
    return (
        json.loads(result_file.read_text(encoding="utf-8")),
        report,
        completed.stdout + completed.stderr,
    )


def test_a_finding_on_a_line_the_pr_did_not_change_loses_its_block(
    tmp_path: Path,
) -> None:
    result, report, log = _run_block(
        tmp_path, _result(_cluster(1, "Loader ignores a missing file"))
    )

    assert result["quorum"]["verdict"] == "passed"
    assert result["quorum"]["blocking_count"] == 0
    assert "dropped blocking finding (pre_existing)" in log
    assert report is not None
    assert [d["disposition"] for d in report["dropped"]] == ["pre_existing"]
    assert report["kept"] == []
    assert report["state"]["round"] == 1


def test_a_finding_on_a_line_the_pr_changed_still_blocks(tmp_path: Path) -> None:
    """Negative control: the same run keeps the finding the pull request introduced."""
    result, report, _ = _run_block(
        tmp_path, _result(_cluster(3, "Loader ignores a missing file"))
    )

    assert result["quorum"]["verdict"] == "blocked"
    assert result["quorum"]["blocking_count"] == 1
    assert report is not None
    assert report["dropped"] == []
    assert report["kept"] == ["app.py:3 Loader ignores a missing file"]


def test_the_previous_round_is_read_from_the_file_the_earlier_step_wrote(
    tmp_path: Path,
) -> None:
    prior = json.dumps(
        {"round": 4, "head": "a" * 40, "posted": [], "posted_before": []}
    )
    _, report, _ = _run_block(
        tmp_path,
        _result(_cluster(3, "Loader ignores a missing file")),
        prior_state=prior,
    )

    assert report is not None
    assert report["state"]["round"] == 5
    assert report["state"]["previous_head"] == "a" * 40


def test_a_base_whose_node_predates_the_policy_still_runs_the_parse_veto(
    tmp_path: Path,
) -> None:
    result, report, log = _run_block(
        tmp_path,
        _result(_cluster(1, "Loader ignores a missing file")),
        base_has_policy=False,
    )

    assert report is None
    assert result["quorum"]["verdict"] == "blocked", (
        "no policy ran, so nothing was dropped"
    )
    assert "did not run" not in log, (
        "the old node must not be handed arguments it rejects"
    )


def test_the_policy_runs_before_the_verdict_is_read() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.index("--prior-state-file") < text.index("VERDICT_DATA=$(")


def _steps() -> dict[str, dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {s["name"]: s for s in workflow["jobs"]["hostile-review"]["steps"]}


def test_only_the_actions_bot_can_set_the_round() -> None:
    steps = _steps()
    reader = steps["Read the previous review round"]["with"]["script"]
    poster = steps["Post review summary as PR comment"]["with"]["script"]
    for script in (reader, poster):
        assert "c.user.login !== 'github-actions[bot]'" in script or (
            "c.user.login === 'github-actions[bot]'" in script
        )
        assert "hostile-review-state:" in script
    names = list(steps)
    assert names.index("Read the previous review round") < names.index(
        "Run adversarial review"
    )


def test_the_comment_keeps_the_state_when_a_run_reports_none() -> None:
    poster = _steps()["Post review summary as PR comment"]["with"]["script"]
    assert "else if (existing)" in poster
    assert "after three review rounds" in poster
