# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Workspace-hygiene guard for sibling clones on persistent runners (OMN-13953).

Self-hosted runners reuse their workspace. The runner job-started hook resets
only ``_work/<repo>/<repo>``, never its parent, so a step that clones a sibling
repo into ``../<name>`` leaves that directory behind for the next job on the
same runner. The following run then dies at ``git clone`` with exit 128 --
``fatal: destination path '../omnibase_core' already exists and is not an empty
directory`` -- before any real check executes.

Live instances this guard is derived from (both on omnimarket@dev):

* ``validator-fsm-handler-drift.yml`` -- 39 consecutive non-green ``push`` runs
  (38 ``failure`` + 1 ``cancelled``) from 2026-07-29T02:42Z (run 30417551946)
  through 2026-07-31T02:22Z (run 30598791019). ``pull_request`` runs were
  unaffected because the runner selector sends them to ephemeral
  ``ubuntu-latest``, which is why the red was invisible on PRs and only ever
  showed up on the post-merge trunk signal. Now fixed: the first post-merge
  ``push`` run (30606643966, merge commit 0f459034) is green.
* ``delegation-regression-nightly.yml`` -- 21 of its 37 ``schedule`` runs died
  at this class, most recently a 5-run streak 2026-07-26..2026-07-30 (run
  30526140685 and predecessors). That workflow has never had a green run, but
  the other 15 failures are a *different*, still-open blocker at a later step
  (missing ``STABILITY_TEST_POSTGRES_PASSWORD``, tracked in OMN-14256), so
  clearing this class does not by itself turn that nightly green.

``plugin-compat-gate.yml`` already carried the fix (a ``rm -rf`` cleanup step
added in de84a5f3) and stayed green throughout; this test generalizes that
one-off into an invariant so the class cannot silently recur.

Invariant: within a job that can land on a persistent self-hosted runner, every
``git clone ... ../<dir>`` must be preceded, in execution order, by an
``rm -rf`` of that same ``../<dir>``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# A job runs on a reused workspace if its runs-on can resolve to the self-hosted
# fleet -- either a literal label, or the trusted-CI selector var whose
# documented fallback is ["self-hosted","omnibase-ci"].
_PERSISTENT_MARKERS = ("self-hosted", "OMNI_TRUSTED_CI_RUNS_ON_JSON")

_CLONE_RE = re.compile(r"git\s+clone\b[^\n]*?\s\.\./([A-Za-z0-9_.\-]+)")
_RM_RE = re.compile(r"rm\s+-rf\b([^\n]*)")
_RM_TARGET_RE = re.compile(r"\.\./([A-Za-z0-9_.\-]+)")


def _runs_on_is_persistent(runs_on: object) -> bool:
    return any(marker in str(runs_on) for marker in _PERSISTENT_MARKERS)


def unclean_clones(workflow_text: str) -> list[tuple[str, str]]:
    """Return (job_name, sibling_dir) for each clone not preceded by its cleanup."""
    doc = yaml.safe_load(workflow_text) or {}
    findings: list[tuple[str, str]] = []

    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if not _runs_on_is_persistent(job.get("runs-on", "")):
            continue

        cleaned: set[str] = set()
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            # Order matters inside a single run block too, so walk line by line.
            for line in run.splitlines():
                removal = _RM_RE.search(line)
                if removal:
                    cleaned.update(_RM_TARGET_RE.findall(removal.group(1)))
                for target in _CLONE_RE.findall(line):
                    if target not in cleaned:
                        findings.append((job_name, target))
    return findings


SELF_HOSTED_JOB = """
jobs:
  probe:
    runs-on: ["self-hosted", "omnibase-ci"]
    steps:
      - uses: actions/checkout@v7
{body}
"""

_CLONE_CORE = (
    "git clone --depth=1 https://example.invalid/omnibase_core.git ../omnibase_core"
)
_CLONE_MEMORY = (
    "git clone --depth=1 https://example.invalid/omnimemory.git ../omnimemory"
)


class TestDetector:
    """RED/GREEN proof against synthesized workflows."""

    def test_flags_clone_without_cleanup(self) -> None:
        workflow = SELF_HOSTED_JOB.format(body=f"      - run: {_CLONE_CORE}\n")
        assert unclean_clones(workflow) == [("probe", "omnibase_core")]

    def test_accepts_cleanup_in_earlier_step(self) -> None:
        workflow = SELF_HOSTED_JOB.format(
            body=(f"      - run: rm -rf ../omnibase_core\n      - run: {_CLONE_CORE}\n")
        )
        assert unclean_clones(workflow) == []

    def test_accepts_cleanup_earlier_in_same_run_block(self) -> None:
        workflow = SELF_HOSTED_JOB.format(
            body=(
                "      - run: |\n"
                "          rm -rf ../omnibase_core ../omnimemory\n"
                f"          {_CLONE_CORE}\n"
                f"          {_CLONE_MEMORY}\n"
            )
        )
        assert unclean_clones(workflow) == []

    def test_cleanup_after_the_clone_does_not_count(self) -> None:
        workflow = SELF_HOSTED_JOB.format(
            body=(f"      - run: {_CLONE_CORE}\n      - run: rm -rf ../omnibase_core\n")
        )
        assert unclean_clones(workflow) == [("probe", "omnibase_core")]

    def test_partial_cleanup_flags_only_the_uncleaned_sibling(self) -> None:
        workflow = SELF_HOSTED_JOB.format(
            body=(
                "      - run: rm -rf ../omnibase_core\n"
                "      - run: |\n"
                f"          {_CLONE_CORE}\n"
                f"          {_CLONE_MEMORY}\n"
            )
        )
        assert unclean_clones(workflow) == [("probe", "omnimemory")]

    def test_ephemeral_runner_is_exempt(self) -> None:
        workflow = f"""
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - run: {_CLONE_CORE}
"""
        assert unclean_clones(workflow) == []

    def test_trusted_ci_selector_counts_as_persistent(self) -> None:
        workflow = f"""
jobs:
  probe:
    runs-on: >-
      ${{{{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON) }}}}
    steps:
      - run: {_CLONE_CORE}
"""
        assert unclean_clones(workflow) == [("probe", "omnibase_core")]


@pytest.mark.parametrize(
    "workflow", sorted(WORKFLOW_DIR.glob("*.yml")), ids=lambda path: path.name
)
def test_repo_workflows_clean_siblings_before_cloning(workflow: Path) -> None:
    findings = unclean_clones(workflow.read_text(encoding="utf-8"))
    assert not findings, (
        f"{workflow.name}: sibling clone(s) on a persistent self-hosted runner "
        f"without a preceding `rm -rf`: {findings}. The runner job-started hook "
        "does not reset the workspace parent, so the next run on that runner "
        "fails at `git clone` with exit 128. Add a 'Clean stale dependency "
        "clones' step before the clones (see plugin-compat-gate.yml)."
    )
