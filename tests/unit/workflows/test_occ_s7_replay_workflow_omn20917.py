# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OCC S7 replay workflow is read-only, dispatch-only and fails closed (OMN-20917)."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "occ-s7-replay.yml"
TARGET_REPOSITORIES = [
    "omnibase_compat",
    "omnimemory",
    "omniintelligence",
    "omnibase_core",
    "omnibase_infra",
    "omniclaude",
    "omnibase_spi",
]
pytestmark = pytest.mark.unit


def test_merged_pr_extraction_drains_history_under_pipefail(tmp_path: Path) -> None:
    """Execute the workflow pipeline with enough output to overflow a pipe."""
    git = ["git", f"--git-dir={tmp_path / '.git'}", f"--work-tree={tmp_path}"]
    subprocess.run([*git, "init", "--quiet"], check=True, timeout=10)
    numbers = list(range(1000000, 1012000))
    commits = "".join(
        f"commit refs/remotes/origin/dev\n"
        f"committer Replay Test <replay@example.com> {1700000000 + i} +0000\n"
        f"data {len(subject)}\n{subject}\n\n"
        for i, number in enumerate(numbers)
        for subject in [f"replay subject (#{number})"]
    )
    subprocess.run(
        [*git, "fast-import", "--quiet"],
        input=commits,
        text=True,
        check=True,
        timeout=30,
    )
    step = next(s for s in _steps(_load()) if s["name"].startswith("Clone"))
    run = step["run"]
    extraction = run[run.index("git log --first-parent") : run.index("test -s")]
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", extraction],
        env={
            **os.environ,
            "GIT_DIR": str(tmp_path / ".git"),
            "GIT_WORK_TREE": str(tmp_path),
            "branch": "origin/dev",
            "RUNNER_TEMP": str(tmp_path),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "merged_prs.txt").read_text().splitlines() == [
        str(n) for n in reversed(numbers[-200:])
    ]


@pytest.mark.parametrize(
    ("declarations", "expected"),
    [
        (
            {
                "caller.yml": {
                    "jobs": {
                        "repo-evidence": {
                            "uses": "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml@abc",
                            "with": {"occ-context": "nested / custom OCC verdict"},
                        }
                    }
                }
            },
            "nested / custom OCC verdict",
        ),
        (
            {
                "receipt-gate.yml": {
                    "on": {
                        "workflow_call": {
                            "inputs": {
                                "occ-context": {
                                    "default": "local / custom OCC verdict"
                                },
                            }
                        }
                    }
                }
            },
            "local / custom OCC verdict",
        ),
        (
            {
                "ci.yml": {
                    "jobs": {
                        "occ-preflight": {
                            "uses": "OmniNode-ai/omnibase_core/.github/workflows/occ-preflight.yml@abc",
                            "name": "repository OCC",
                        }
                    }
                }
            },
            "repository OCC / eligibility",
        ),
        ({}, None),
        (
            {
                "caller.yml": {
                    "jobs": {
                        "repo-evidence": {
                            "uses": "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml@abc",
                            "with": {"occ-context": ""},
                        }
                    }
                }
            },
            None,
        ),
        (
            {
                "caller.yml": {
                    "jobs": {
                        "repo-evidence": {
                            "uses": "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml@abc",
                            "with": {"occ-context": "${{ inputs.context }}"},
                        }
                    }
                }
            },
            None,
        ),
        (
            {
                "caller.yml": {
                    "jobs": {
                        name: {
                            "uses": "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml@abc",
                            "with": {"occ-context": context},
                        }
                        for name, context in [
                            ("one", "one verdict"),
                            ("two", "two verdict"),
                        ]
                    }
                }
            },
            None,
        ),
    ],
    ids=[
        "caller-override",
        "local-default",
        "producer-name",
        "missing",
        "empty",
        "expression",
        "ambiguous",
    ],
)
def test_repository_occ_context_is_resolved_and_fail_closed(
    tmp_path: Path, declarations: dict[str, Any], expected: str | None
) -> None:
    """Run the whole replay step; intercept only the final network replay call."""
    workflows = tmp_path / "registry" / "omnibase_spi" / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for filename, contents in declarations.items():
        (workflows / filename).write_text(yaml.safe_dump(contents), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:3] == ['run', 'python', '-']:\n"
        "    sys.exit(subprocess.call([sys.executable, *args[2:]]))\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps(args))\n"
        "sys.exit(int(os.environ['REPLAY_EXIT']))\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    capture = tmp_path / "argv.json"
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _replay_step(_load())["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REPOSITORY": "omnibase_spi",
            "OMNI_HOME": str(tmp_path / "registry"),
            "RUNNER_TEMP": str(tmp_path),
            "CAPTURE": str(capture),
            "REPLAY_EXIT": "2",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if expected is None:
        assert proc.returncode != 0, proc.stdout
        assert not capture.exists(), "unresolved context must stop before replay"
    else:
        assert proc.returncode == 2, proc.stderr
        argv = json.loads(capture.read_text())
        assert "--occ-context" in argv, argv
        assert argv[argv.index("--occ-context") + 1] == expected


def _load() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers: dict[str, Any] = workflow["on"] if "on" in workflow else workflow[True]
    return triggers


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    jobs = workflow["jobs"]
    assert list(jobs) == ["replay"]
    steps: list[dict[str, Any]] = jobs["replay"]["steps"]
    return steps


def _replay_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    return next(
        s for s in _steps(workflow) if "occ-difference replay" in s.get("run", "")
    )


def _violations(workflow: dict[Any, Any]) -> list[str]:
    """Every way the workflow leaves the read-only, dispatch-only, fail-closed shape."""
    found: list[str] = []
    triggers = _triggers(workflow)
    if set(triggers) != {"workflow_dispatch"}:
        found.append(f"triggers beyond workflow_dispatch: {sorted(triggers)}")
    repository = (
        triggers.get("workflow_dispatch", {}).get("inputs", {}).get("repository", {})
    )
    if (
        repository.get("type") != "choice"
        or repository.get("options") != TARGET_REPOSITORIES
    ):
        found.append(
            "repository input is not the choice of the seven target repositories"
        )
    if workflow.get("permissions") != {"contents": "read"}:
        found.append(f"workflow permissions: {workflow.get('permissions')}")
    job = workflow["jobs"]["replay"]
    if "permissions" in job:
        found.append("job widens permissions")
    if "needs" in job or "strategy" in job:
        found.append("job is not standalone")
    for step in _steps(workflow):
        if "continue-on-error" in step or "continue-on-error" in job:
            found.append(f"continue-on-error on {step.get('name')}")
    text = yaml.safe_dump(workflow)
    for token in ("secrets.", "pull_request", "schedule"):
        if token in text:
            found.append(f"workflow mentions {token}")
    return found


def test_workflow_is_dispatch_only_read_only_and_fail_closed() -> None:
    assert _violations(_load()) == []


def test_repository_input_is_the_seven_target_repositories() -> None:
    inputs = _triggers(_load())["workflow_dispatch"]["inputs"]
    assert inputs["repository"]["required"] is True
    assert inputs["repository"]["options"] == TARGET_REPOSITORIES


def test_replay_runs_the_thirty_pr_window_and_records_both_files() -> None:
    run = _replay_step(_load())["run"]
    assert "python -m omnimarket.nodes.node_dod_verify occ-difference replay" in run
    assert '--repository "OmniNode-ai/${REPOSITORY}"' in run
    assert "--count 30" in run
    assert "--record-to" in run
    assert "rows.json" in run
    assert "--table-to" in run
    assert "table.md" in run
    assert "set -euo pipefail" in run


def test_replay_reads_with_the_default_token_and_an_ephemeral_registry_root() -> None:
    step = _replay_step(_load())
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["OMNI_HOME"] == "${{ runner.temp }}/registry"
    assert "REPOSITORY" in step["env"]
    assert "${{ inputs." not in step["run"], "the input reaches the shell only via env"


def test_summary_and_artifact_run_even_when_the_replay_is_red() -> None:
    steps = _steps(_load())
    summary = next(s for s in steps if s.get("name", "").startswith("Publish"))
    upload = next(
        s
        for s in steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert summary["if"] == "always()"
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    assert "table.md" in summary["run"]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    paths = upload["with"]["path"].split()
    assert any(p.endswith("rows.json") for p in paths)
    assert any(p.endswith("table.md") for p in paths)


def test_summary_and_artifact_publish_table_to_job_log(tmp_path: Path) -> None:
    step = next(s for s in _steps(_load()) if s.get("name", "").startswith("Publish"))
    out_dir = tmp_path / "replay-out"
    out_dir.mkdir()
    table = "| pr | outcome | note |\n| 7 | not_compared | no OCC run on the head |\n"
    (out_dir / "table.md").write_text(table, encoding="utf-8")
    summary = tmp_path / "summary.md"
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_STEP_SUMMARY": str(summary),
            "REPOSITORY": "omnimemory",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert table in proc.stdout
    assert table in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda wf: _triggers(wf).update({"schedule": [{"cron": "0 3 * * *"}]}),
        lambda wf: _triggers(wf).update({"pull_request": {}}),
        lambda wf: wf.update({"permissions": {"contents": "write"}}),
        lambda wf: wf["jobs"]["replay"]["steps"][-1].update(
            {"continue-on-error": True}
        ),
        lambda wf: _triggers(wf)["workflow_dispatch"]["inputs"]["repository"][
            "options"
        ].pop(),
        lambda wf: wf["jobs"]["replay"]["steps"][0].update(
            {"env": {"T": "${{ secrets.NEW_TOKEN }}"}}
        ),
    ],
    ids=[
        "cron",
        "pull_request",
        "write-permission",
        "continue-on-error",
        "fewer-repos",
        "new-secret",
    ],
)
def test_each_forbidden_change_is_caught(mutate: Any) -> None:
    mutated = copy.deepcopy(_load())
    mutate(mutated)
    assert _violations(mutated) != []


def _job_argv(rows_file: Path, out_dir: Path) -> list[str]:
    """The replay step's own command line, reading a recorded rows file."""
    run = _replay_step(_load())["run"].replace("\\\n", " ")
    line = next(part for part in run.splitlines() if "occ-difference replay" in part)
    words = shlex.split(line.replace("${REPOSITORY}", "omnibase_spi"))
    argv = [sys.executable if word == "python" else word for word in words]
    argv = argv[argv.index(sys.executable) :]
    work = argv.index("--work-dir")
    argv[work : work + 2] = ["--rows-file", str(rows_file)]
    return [
        str(out_dir / Path(word).name) if word.startswith("replay-out/") else word
        for word in argv
    ]


def _rows(count: int, disagree_newest: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "pr": 500 - i,
            "head_sha": f"{500 - i:040x}",
            "merged_at": "2026-10-01T00:00:00Z",
            "tickets": [f"OMN-{500 - i}"],
            "occ_check_run": {"conclusion": "success"},
            "new_verdict": {"admitted": not (disagree_newest and i == 0)},
        }
        for i in range(count)
    ]
    return rows


@pytest.mark.parametrize(("disagree", "exit_code"), [(False, 0), (True, 1)])
def test_job_command_exit_code_follows_the_recorded_rows(
    tmp_path: Path, disagree: bool, exit_code: int
) -> None:
    rows_file = tmp_path / "in.json"
    rows_file.write_text(json.dumps(_rows(31, disagree)), encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    argv = _job_argv(rows_file, out_dir)
    assert argv[argv.index("--count") + 1] == "30"
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode == exit_code, proc.stderr
    assert json.loads((out_dir / "rows.json").read_text(encoding="utf-8"))
    table = (out_dir / "table.md").read_text(encoding="utf-8")
    assert ("unclassified_difference" in table) is disagree
