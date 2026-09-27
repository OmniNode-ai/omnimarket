# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin whole-tree CI counterparts for OMN-19612's staged-file hooks.

commit 4d0068508 ("perf(pre-commit): scope validators to staged files")
moved eight local pre-commit hooks from pass_filenames: false (whole tree,
every commit) to pass_filenames: true (only the staged diff), for commit
speed. Whole-tree enforcement still has to happen somewhere or a violation
sitting outside the staged diff is never caught again.

It already does, for every one of the eight, in two existing CI jobs:
`lint` in ci.yml (STRICT_GATE_JOBS in scripts/ci/ci_summary_gate.py) and
`handler-event-type-source` in contract-topic-closure.yml (asserted via the
CI Summary's Layer-4 EXPECTED_EXTERNAL_CONTEXTS). This module pins that
coverage so it cannot silently regress, and asserts the shape generically
(every pass_filenames: true hook needs a counterpart) so a NEW staged-scoped
hook with no whole-tree run also fails, rather than only re-checking this
specific list forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXPECTED_EXTERNAL_CONTEXTS,
    SOFT_ALLOWLIST,
    STRICT_GATE_JOBS,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TOPIC_CLOSURE_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "contract-topic-closure.yml"
)

# hook_id -> (workflow file, job_id, required-fragment(s) that must appear in
# that job's run: commands). The fragment proves the whole-tree run is still
# wired to the SAME underlying check, not just present under the same name.
BACKSTOPS: dict[str, tuple[Path, str, tuple[str, ...]]] = {
    "handler-event-type-source": (
        TOPIC_CLOSURE_WORKFLOW,
        "handler-event-type-source",
        ("omnimarket.validators.handler_event_type_source",),
    ),
    "aiokafka-construction-auth": (
        CI_WORKFLOW,
        "lint",
        ("scripts/ci/check_aiokafka_construction_auth.py",),
    ),
    "projection-dlq-path": (
        CI_WORKFLOW,
        "lint",
        ("scripts/ci/check_projection_dlq_path.py",),
    ),
    "github-token-env-gate": (
        CI_WORKFLOW,
        "lint",
        ("scripts/ci/check_github_token_env_reads.py",),
    ),
    "delegation-env-read-gate": (
        CI_WORKFLOW,
        "lint",
        ("scripts/ci/check_delegation_env_reads.py",),
    ),
    "watchdog-topic-authority-gate": (
        CI_WORKFLOW,
        "lint",
        ("scripts/ci/check_watchdog_topic_authority.py",),
    ),
    "validate-no-env-fallbacks": (
        CI_WORKFLOW,
        "lint",
        ("scripts/validation/validate_no_env_fallbacks.py",),
    ),
    "ruff-format": (
        CI_WORKFLOW,
        "lint",
        ("ruff format --check src/ tests/",),
    ),
    "ruff": (
        CI_WORKFLOW,
        "lint",
        ("ruff check src/ tests/",),
    ),
}

# Jobs whose check-run only ever appears through CI Summary's Layer-4
# EXPECTED_EXTERNAL_CONTEXTS assertion (they live in a workflow file the
# in-run poller layers 1-3 never see) rather than STRICT_GATE_JOBS.
EXTERNAL_REQUIRED_JOBS = frozenset({"handler-event-type-source"})

# These were already pass_filenames: true before commit 4d0068508
# ("perf(pre-commit): scope validators to staged files (OMN-19612)") -- not
# moved to staged scope by this PR, so a whole-tree backstop for them is out
# of scope here. Recorded explicitly so a real new gap cannot hide behind
# "it was probably already like that".
PRE_EXISTING_STAGED_HOOKS = frozenset(
    {
        "check-no-faked-boundary",
        "entry-point-format",
        "operation-match-requires-operation",
        "reject-deploy-gate-skip-token",
        "shell-hygiene",
    }
)


def _load_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.name} did not parse to a mapping"
    return loaded


def _staged_scoped_hook_ids() -> set[str]:
    """Hooks pre-commit hands only the staged diff at commit time."""
    config = _load_yaml(PRECOMMIT_CONFIG)
    ids: set[str] = set()
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            stages = hook.get("stages", config.get("default_stages", ["pre-commit"]))
            if "pre-commit" not in stages:
                continue
            if hook.get("pass_filenames") is True:
                ids.add(str(hook["id"]))
    return ids


def _job(workflow_path: Path, job_id: str) -> dict:
    workflow = _load_yaml(workflow_path)
    jobs = workflow["jobs"]
    assert job_id in jobs, f"{workflow_path.name} has no `{job_id}` job"
    return jobs[job_id]


def _commands(workflow_path: Path, job_id: str) -> str:
    return "\n".join(
        str(step.get("run", "")) for step in _job(workflow_path, job_id)["steps"]
    )


def _pull_request_trigger(workflow_path: Path) -> dict:
    workflow = _load_yaml(workflow_path)
    triggers = workflow[True] if True in workflow else workflow["on"]
    return triggers.get("pull_request") or {}


def test_every_staged_scoped_hook_has_a_declared_backstop() -> None:
    staged = _staged_scoped_hook_ids()
    assert staged, "expected at least one staged-file-scoped hook"
    missing = staged - set(BACKSTOPS) - PRE_EXISTING_STAGED_HOOKS
    assert not missing, (
        f"these staged-scoped hooks have no declared whole-tree backstop: {sorted(missing)}"
    )
    assert set(BACKSTOPS) <= staged, (
        "a BACKSTOPS entry no longer names a staged-scoped hook -- prune it"
    )


@pytest.mark.parametrize(("hook_id", "backstop"), BACKSTOPS.items())
def test_whole_tree_counterpart_is_present_and_fail_closed(
    hook_id: str, backstop: tuple[Path, str, tuple[str, ...]]
) -> None:
    workflow_path, job_id, fragments = backstop
    job = _job(workflow_path, job_id)
    commands = _commands(workflow_path, job_id)
    for fragment in fragments:
        assert fragment in commands, f"{hook_id} lost whole-tree fragment {fragment!r}"

    assert job.get("continue-on-error") is not True
    for step in job["steps"]:
        assert step.get("continue-on-error") is not True, (
            f"a step of {job_id} ({workflow_path.name}) is continue-on-error, so "
            f"{hook_id}'s whole-tree run can fail silently"
        )
    # `if: always()` on the job itself is not a skip condition (it runs even
    # when its `needs` failed); a job-level `if:` of anything else would be.
    job_if = job.get("if")
    assert job_if in (None, "always()"), (
        f"{job_id} acquired a conditional if: {job_if!r}"
    )


@pytest.mark.parametrize(("hook_id", "backstop"), BACKSTOPS.items())
def test_backstop_job_is_required(
    hook_id: str, backstop: tuple[Path, str, tuple[str, ...]]
) -> None:
    _workflow_path, job_id, _fragments = backstop
    job_name = str(_job(*backstop[:2]).get("name", job_id))
    if job_id in EXTERNAL_REQUIRED_JOBS:
        assert (
            job_id in EXPECTED_EXTERNAL_CONTEXTS
            or job_name in EXPECTED_EXTERNAL_CONTEXTS
        ), (
            f"{hook_id}'s backstop job {job_id!r} is not asserted by CI Summary's "
            "Layer-4 EXPECTED_EXTERNAL_CONTEXTS"
        )
    else:
        assert job_id in STRICT_GATE_JOBS, (
            f"{hook_id}'s backstop job {job_id!r} is not in STRICT_GATE_JOBS"
        )
        assert job_id not in SOFT_ALLOWLIST
        assert job_name not in SOFT_ALLOWLIST


def test_workflows_carry_no_pull_request_paths_filter() -> None:
    for workflow_path in {CI_WORKFLOW, TOPIC_CLOSURE_WORKFLOW}:
        pull_request = _pull_request_trigger(workflow_path)
        assert "paths" not in pull_request, (
            f"{workflow_path.name} gained a paths filter"
        )
        assert "paths-ignore" not in pull_request, (
            f"{workflow_path.name} gained a paths-ignore filter"
        )
