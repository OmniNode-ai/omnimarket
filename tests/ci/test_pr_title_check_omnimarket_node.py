# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21001: preserve the title context and execute a published decision node."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/pr-title-check.yml"
MANIFEST = ROOT / ".github/required-checks.yaml"
CONTEXT = "pr-title / check-title"
NODE_MODULE = "omnimarket.nodes.node_pr_title_check_compute"
RELEASE = "0.4.310"
RUNS_ON = """${{
  (github.event_name == 'pull_request' &&
   github.event.pull_request.head.repo.full_name != github.repository)
  && fromJSON(vars.OMNI_PUBLIC_PR_RUNS_ON_JSON || '["ubuntu-latest"]')
  || fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON || '["ubuntu-latest"]')
}}"""

# Decision statements from the caller's pinned reusable, 6adc28168d023d027bf9c1172f262ac03c9d9055.
# Execute bash rather than a Python reimplementation of its matching rules.
OLD_BASH = r"""
if [[ -z "$PR_TITLE" ]]; then exit 1; fi
TITLE_LOWER=$(echo "$PR_TITLE" | tr '[:upper:]' '[:lower:]')
if [[ "$PR_AUTHOR" == *"[bot]" ]]; then exit 0; fi
if [[ "$TITLE_LOWER" =~ ^(chore\(deps|build\(deps|bump ) ]]; then exit 0; fi
if [[ "$TITLE_LOWER" =~ ^(chore:\ release|chore\(release\)|release:) ]]; then exit 0; fi
if echo "$PR_TITLE" | grep -qE 'OMN-[0-9]+'; then exit 0; fi
exit 1
"""


def _workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return loaded


def _job() -> dict[str, Any]:
    job: dict[str, Any] = _workflow()["jobs"]["pr-title"]
    return job


def _pinned_release() -> str:
    """The release the workflow installs, read from its own step environment."""
    release: str = _job()["steps"][1]["env"]["OMNIMARKET_VERSION"]
    return release


def test_workflow_no_longer_calls_change_control() -> None:
    assert "onex_change_control" not in WORKFLOW.read_text()
    assert "uses" not in _job()


def test_job_keeps_context_triggers_and_runner_routing() -> None:
    job = _job()
    assert job["name"] == CONTEXT
    assert job["runs-on"] == RUNS_ON
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {"contents": "read"}
    assert "if" not in job
    assert "needs" not in job
    assert _workflow()[True]["pull_request"]["types"] == [
        "opened",
        "edited",
        "synchronize",
        "reopened",
    ]


def test_job_runs_the_published_node_without_checkout() -> None:
    steps = _job()["steps"]
    assert len(steps) == 2
    bootstrap = steps[0]
    assert all("uses" not in step for step in steps)
    assert bootstrap["env"]["UV_VERSION"] == "0.12.10"
    assert "python3 -m venv" in bootstrap["run"]
    assert "uv==${UV_VERSION}" in bootstrap["run"]
    assert "${GITHUB_PATH}" in bootstrap["run"]
    assert "set -euo pipefail" in bootstrap["run"]
    assert all("checkout" not in step.get("uses", "") for step in steps)
    env = steps[1]["env"]
    assert env["OMNIMARKET_VERSION"] == RELEASE
    assert env["PR_TITLE"] == "${{ github.event.pull_request.title }}"
    assert env["PR_AUTHOR"] == "${{ github.event.pull_request.user.login }}"
    script = steps[1]["run"]
    assert f"-I -m {NODE_MODULE}" in script
    assert '--no-deps "omnimarket==${OMNIMARKET_VERSION}"' in script
    assert '"pydantic>=2.11"' in script
    assert 'uv venv --quiet --python 3.12 "${venv}"' in script
    assert 'os.environ["PR_TITLE"]' in script
    assert 'os.environ["PR_AUTHOR"]' in script
    assert "set -euo pipefail" in script
    assert "continue-on-error" not in WORKFLOW.read_text()


def test_manifest_row_names_the_local_producer() -> None:
    gates = yaml.safe_load(MANIFEST.read_text())["gates"]
    rows = [g for g in gates if g["name"] == CONTEXT]
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "REQUIRED"
    assert row["branch"] == "dev"
    assert row["producer_kind"] == "local"
    assert row["workflow"] == "pr-title-check.yml"
    assert row["job_path"] == ["pr-title"]
    assert row["skip_semantics"] == "never"
    assert not {"caller_workflow", "caller_job", "cross_repo_ref"} & row.keys()
    assert "onex_change_control" not in row["rationale"]


@pytest.fixture(scope="module")
def release_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install the workflow's pinned wheel; isolated mode excludes this PR's source."""
    release = _pinned_release()
    venv = tmp_path_factory.mktemp("pr-title-release") / "venv"
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(venv)], check=True, timeout=30
    )
    python = venv / "bin/python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--index-url",
            "https://pypi.org/simple",
            f"omnimarket=={release}",
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "pydantic>=2.11"],
        check=True,
        timeout=60,
    )
    metadata = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            'import importlib.metadata as m; print(m.version("omnimarket"))',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert metadata.stdout.strip() == release
    return python


TITLE_CASES = [
    pytest.param("fix(OMN-21001): title check", "human", 0, id="omn-title"),
    pytest.param("fix title check", "human", 1, id="bare-title"),
    pytest.param("fix title check", "dependabot[bot]", 0, id="bot-suffix"),
    pytest.param("chore(deps): upgrade", "human", 0, id="chore-deps"),
    pytest.param("build(deps): upgrade", "human", 0, id="build-deps"),
    pytest.param("bump a library", "human", 0, id="bump"),
    pytest.param("chore: release package", "human", 0, id="chore-release"),
    pytest.param("chore(release): package", "human", 0, id="chore-release-scope"),
    pytest.param("release: package", "human", 0, id="release"),
    pytest.param("CHORE(DEPS): upgrade", "human", 0, id="upper-chore-deps"),
    pytest.param("BUILD(DEPS): upgrade", "human", 0, id="upper-build-deps"),
    pytest.param("BUMP a library", "human", 0, id="upper-bump"),
    pytest.param("CHORE: RELEASE package", "human", 0, id="upper-chore-release"),
    pytest.param("CHORE(RELEASE): package", "human", 0, id="upper-release-scope"),
    pytest.param("RELEASE: package", "human", 0, id="upper-release"),
    pytest.param("fix omn-21001", "human", 1, id="lowercase-ticket"),
    pytest.param("fix OMN-x", "human", 1, id="nonnumeric-ticket"),
    pytest.param(" chore(deps): upgrade", "human", 1, id="leading-space"),
    pytest.param("fix title check", "human[bot]suffix", 1, id="bot-not-suffix"),
    pytest.param("fix title check", "robot[BOT]", 1, id="bot-case-sensitive"),
    pytest.param("", "dependabot[bot]", 1, id="empty-before-exemption"),
]


@pytest.fixture(scope="module")
def release_verdicts(release_python: Path) -> dict[tuple[str, str], tuple[int, str]]:
    """Exercise the installed CLI entry point for every case in one interpreter."""
    payloads = [
        {"title": str(case.values[0]), "author": str(case.values[1])}
        for case in TITLE_CASES
    ]
    script = """
import contextlib
import io
import json
import sys
from omnimarket.nodes.node_pr_title_check_compute.__main__ import main

payloads = json.load(sys.stdin)
verdicts = []
for payload in payloads:
    sys.stdin = io.StringIO(json.dumps(payload))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = main()
    verdicts.append([exit_code, output.getvalue()])
print(json.dumps(verdicts))
"""
    result = subprocess.run(
        [str(release_python), "-I", "-c", script],
        input=json.dumps(payloads),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    verdicts = json.loads(result.stdout)
    return {
        (payload["title"], payload["author"]): (verdict[0], verdict[1])
        for payload, verdict in zip(payloads, verdicts, strict=True)
    }


@pytest.mark.parametrize(("title", "author", "expected"), TITLE_CASES)
def test_installed_node_matches_old_bash(
    release_verdicts: dict[tuple[str, str], tuple[int, str]],
    title: str,
    author: str,
    expected: int,
) -> None:
    old = subprocess.run(
        ["bash", "-c", OLD_BASH],
        env={**os.environ, "PR_TITLE": title, "PR_AUTHOR": author},
        capture_output=True,
        text=True,
        timeout=10,
    )
    exit_code, output = release_verdicts[(title, author)]
    assert old.returncode == expected, old.stderr
    assert exit_code == old.returncode, output
