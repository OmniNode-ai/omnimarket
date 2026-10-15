# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20073 AC2: only GitHub Actions' success satisfies repo-evidence / dod-verify.

dev's ruleset requires ``repo-evidence / dod-verify`` from GitHub Actions
(integration 15368), and GitHub counts a ``neutral`` or ``skipped`` conclusion
as passing a required context. Two jobs in this repository report that context:
the caller (call-repo-evidence-gate.yml), whose reusable dod-verify job always
runs in caller mode, and the merge-queue shim (merge-group-pr-scoped-contexts.yml),
which re-reports the queued pull request head's verdict on the queue commit.

The shim therefore must not turn a ``neutral`` or ``skipped`` head verdict into
a queue-commit success, and must not take the verdict from a check run of the
same name written by any App other than GitHub Actions. These tests run the
shim's own shell and jq selector over check-run fixtures, with only the network
replaced, and read every workflow for a third producer of the context.
"""

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
WORKFLOWS = ROOT / ".github" / "workflows"
CONTEXT = "repo-evidence / dod-verify"
GITHUB_ACTIONS_APP_ID = 15368
OTHER_APP_ID = 99999
CALLER = ("call-repo-evidence-gate.yml", "repo-evidence")
SHIM = ("merge-group-pr-scoped-contexts.yml", "ctx-repo-evidence-dod-verify")
PR_NUMBER = 4242
HEAD_SHA = "b" * 40
QUEUE_REF = f"refs/heads/gh-readonly-queue/dev/pr-{PR_NUMBER}-{'c' * 40}"


def _load(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path.name
    return loaded


def _workflows() -> dict[str, dict[Any, Any]]:
    return {
        path.name: _load(path)
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        if isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)
    }


def context_producers(workflows: dict[str, dict[Any, Any]]) -> set[tuple[str, str]]:
    """Every (workflow, job id) whose check run can carry the evidence context.

    A plain job reports its ``name`` (or its id); a reusable-workflow caller
    reports ``<name or id> / <called job>``, so any caller displayed as
    ``repo-evidence`` can report ``repo-evidence / dod-verify``.
    """
    producers = set()
    for filename, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            shown = " ".join(str(job.get("name", job_id)).split()).casefold()
            if shown == CONTEXT.casefold() or (
                "uses" in job and shown == "repo-evidence"
            ):
                producers.add((filename, job_id))
    return producers


def check_write_grants(workflows: dict[str, dict[Any, Any]]) -> list[str]:
    """Grants that let a workflow's token write a check run or commit status."""

    def grants(permissions: object) -> list[str]:
        if permissions == "write-all":
            return ["write-all"]
        if isinstance(permissions, dict):
            return [
                scope
                for scope in ("checks", "statuses")
                if permissions.get(scope) == "write"
            ]
        return []

    found = []
    for filename, workflow in workflows.items():
        found += [f"{filename}: {g}" for g in grants(workflow.get("permissions"))]
        for job_id, job in (workflow.get("jobs") or {}).items():
            if isinstance(job, dict):
                found += [
                    f"{filename}:{job_id}: {g}" for g in grants(job.get("permissions"))
                ]
    return found


def _shim_script() -> str:
    job = _load(WORKFLOWS / SHIM[0])["jobs"][SHIM[1]]
    assert job["name"] == CONTEXT
    (step,) = job["steps"]
    assert step["env"]["CONTEXT"] == CONTEXT
    script = step["run"]
    assert isinstance(script, str)
    return script


def _run_shim(checks: list[tuple[int, int, str, str | None]], tmp_path: Path) -> int:
    fixture = tmp_path / "check-runs.json"
    fixture.write_text(
        json.dumps(
            {
                "check_runs": [
                    {
                        "id": identifier,
                        "name": CONTEXT,
                        "app": {"id": app},
                        "status": status,
                        "conclusion": conclusion,
                    }
                    for identifier, app, status, conclusion in checks
                ]
            }
        ),
        encoding="utf-8",
    )
    # The shell runs the workflow's own jq selector over the fixture. Only the
    # network is replaced, and an unexpected API path fails the step.
    prefix = """
gh() {
    local path="" query="" previous=""
    for arg in "$@"; do
        [[ "$previous" == "--jq" ]] && query="$arg"
        [[ "$arg" == repos/* ]] && path="$arg"
        previous="$arg"
    done
    case "$path" in
        "repos/${REPO}/pulls/${PR_NUMBER}") echo "$HEAD_SHA" ;;
        "repos/${REPO}/commits/${HEAD_SHA}/check-runs") jq -r "$query" "$GH_FIXTURE" ;;
        *) echo "unexpected API path: $path" >&2; return 1 ;;
    esac
}
"""
    result = subprocess.run(
        ["bash", "-c", prefix + _shim_script()],
        env={
            **os.environ,
            "GH_FIXTURE": str(fixture),
            "REPO": "OmniNode-ai/omnimarket",
            "QUEUE_REF": QUEUE_REF,
            "CONTEXT": CONTEXT,
            "PR_NUMBER": str(PR_NUMBER),
            "HEAD_SHA": HEAD_SHA,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "unexpected API path" not in result.stderr, result.stderr
    return result.returncode


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([(1, GITHUB_ACTIONS_APP_ID, "completed", "success")], 0),
        ([(1, GITHUB_ACTIONS_APP_ID, "completed", "neutral")], 1),
        ([(1, GITHUB_ACTIONS_APP_ID, "completed", "skipped")], 1),
        ([(1, GITHUB_ACTIONS_APP_ID, "completed", "failure")], 1),
        ([(1, GITHUB_ACTIONS_APP_ID, "completed", "cancelled")], 1),
        ([(1, GITHUB_ACTIONS_APP_ID, "in_progress", None)], 1),
        ([], 1),
        ([(1, OTHER_APP_ID, "completed", "success")], 1),
        (
            [
                (1, GITHUB_ACTIONS_APP_ID, "completed", "failure"),
                (2, OTHER_APP_ID, "completed", "success"),
            ],
            1,
        ),
        (
            [
                (1, GITHUB_ACTIONS_APP_ID, "completed", "success"),
                (2, GITHUB_ACTIONS_APP_ID, "completed", "skipped"),
            ],
            1,
        ),
        (
            [
                (1, GITHUB_ACTIONS_APP_ID, "completed", "success"),
                (2, OTHER_APP_ID, "completed", "failure"),
            ],
            0,
        ),
    ],
)
def test_queue_shim_admits_only_a_github_actions_success(
    checks: list[tuple[int, int, str, str | None]], expected: int, tmp_path: Path
) -> None:
    assert _run_shim(checks, tmp_path) == expected


def test_evidence_context_has_only_the_caller_and_the_queue_shim() -> None:
    assert context_producers(_workflows()) == {CALLER, SHIM}


def test_evidence_context_producer_planted_payload_is_found() -> None:
    planted = {
        "forge.yml": {
            "jobs": {
                "plain": {"name": "repo-evidence /  dod-verify"},
                "repo-evidence": {"uses": "elsewhere/x.yml@main"},
                "renamed": {"name": "Repo-Evidence", "uses": "elsewhere/x.yml@main"},
                "unrelated": {"name": "repo-evidence"},
            }
        }
    }
    assert context_producers(planted) == {
        ("forge.yml", "plain"),
        ("forge.yml", "repo-evidence"),
        ("forge.yml", "renamed"),
    }


def test_no_workflow_grants_a_check_run_or_status_write() -> None:
    assert check_write_grants(_workflows()) == []


def test_check_write_grant_planted_payload_is_found() -> None:
    planted = {
        "a.yml": {"permissions": "write-all", "jobs": {}},
        "b.yml": {"jobs": {"j": {"permissions": {"checks": "write"}}}},
        "c.yml": {"permissions": {"statuses": "write", "contents": "read"}},
        "d.yml": {"permissions": {"checks": "read"}, "jobs": {"j": {}}},
    }
    assert check_write_grants(planted) == [
        "a.yml: write-all",
        "b.yml:j: checks",
        "c.yml: statuses",
    ]


def test_caller_never_runs_the_reusable_in_shadow_mode() -> None:
    """In shadow mode the reusable's dod-verify passes whatever its verdict."""
    job = _load(WORKFLOWS / CALLER[0])["jobs"][CALLER[1]]
    assert job["with"]["evidence-source"] == "caller"
    assert str(job["with"].get("shadow", "false")) == "false"
