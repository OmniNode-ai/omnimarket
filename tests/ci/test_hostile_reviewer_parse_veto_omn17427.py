# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-reviewer workflow vets 'invalid file' findings before the verdict (OMN-17427).

The workflow's veto shell block is EXTRACTED and EXECUTED against a scratch git
repository: a grep for the node's name would pass on a comment. Three properties:

* the veto runs before the verdict is read from the result file, and from the
  base commit, so a pull request cannot supply the code that vets its findings;
* a blocking 'invalid Python' finding on a file that parses at the head is dropped
  and the verdict read afterwards is ``passed``;
* a base commit without the node leaves the result file exactly as it was.
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
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "hostile-reviewer.yml"
NODE = "node_review_finding_veto_effect"
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE


def _veto_block() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index('          VETO_BASE="')
    end = text.index("          # Parse verdict from JSON using Python inline.", start)
    return "\n".join(line[10:] for line in text[start:end].splitlines())


def test_the_veto_runs_before_the_verdict_is_read() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.index('VETO_BASE="') < text.index("VERDICT_DATA=$(")
    assert text.index("uv run python -m omniintelligence.review_pairing.cli_review") < (
        text.index('VETO_BASE="')
    )


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


def _result(path: str) -> dict[str, Any]:
    message = "Test file is syntactically invalid Python | detail"
    cluster = {
        "file_path": path, "line_start": 1, "severity": "error",
        "rule": "category:correctness", "message": message,
        "agreeing_models": ["a", "b"], "agreement_count": 2, "blocking": True,
        "finding_ids": ["a1", "b1"],
    }  # fmt: skip
    return {
        "models_succeeded": ["a", "b"],
        "results": [
            {"model": "a", "findings": [{"finding_id": "a1", "normalized_message": message}]},
            {"model": "b", "findings": [{"finding_id": "b1", "normalized_message": message}]},
        ],
        "quorum": {
            "verdict": "blocked", "blocking_count": 1, "warning_count": 0,
            "blocking_findings": [cluster], "warning_findings": [],
        },
    }  # fmt: skip


def _run_block(tmp_path: Path, *, base_has_node: bool) -> tuple[dict[str, Any], str]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    if base_has_node:
        shutil.copytree(
            NODE_DIR,
            work / "src" / "omnimarket" / "nodes" / NODE,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (work / "src" / "omnimarket" / "__init__.py").write_text("", encoding="utf-8")
        (work / "src" / "omnimarket" / "nodes" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
    base = _commit(work, "base")
    (work / "tests").mkdir()
    (work / "tests" / "test_fill.py").write_text(
        "def test_a():\n    assert 1\n", encoding="utf-8"
    )
    head = _commit(work, "head")

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(_result("tests/test_fill.py")), encoding="utf-8")
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
    return json.loads(
        result_file.read_text(encoding="utf-8")
    ), completed.stdout + completed.stderr


def test_a_parsing_file_loses_its_invalid_python_block(tmp_path: Path) -> None:
    result, log = _run_block(tmp_path, base_has_node=True)

    assert result["quorum"]["verdict"] == "passed"
    assert result["quorum"]["blocking_count"] == 0
    assert "dropped blocking finding tests/test_fill.py:1" in log


def test_a_base_without_the_node_leaves_the_result_untouched(tmp_path: Path) -> None:
    result, log = _run_block(tmp_path, base_has_node=False)

    assert result["quorum"]["verdict"] == "blocked"
    assert result["quorum"]["blocking_count"] == 1
    assert f"carries no {NODE} yet" in log
