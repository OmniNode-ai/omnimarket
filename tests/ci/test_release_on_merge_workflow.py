# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural proof for .github/workflows/release-on-merge.yml (OMN-18010).

A release workflow is the one workflow whose defects are hardest to observe: it
does not run on pull requests, so a mistake in it is invisible until a merge
either publishes the wrong thing or silently publishes nothing. The unit tests in
``tests/unit/scripts/test_decide_release_on_merge.py`` pin the DECISION; this
module pins the parts of the wiring that no unit test can reach — the trigger,
the concurrency contract, the job graph, and the pin ratchet.

Every assertion here corresponds to a measured failure:

  * ``cancel-in-progress: false`` — a cancelled release can leave a pushed tag
    with nothing published behind it.
  * a ``paths:`` filter AND a re-derived changed set — a push can carry several
    commits, so the filter alone is not sufficient (it is the cheap pre-check).
  * the ``[release-on-merge]`` self-push marker on the reopen commit and NOT on
    the arm-dev commit — the reopen commit touches pyproject.toml and so matches
    this workflow's own filter; without the marker it re-enters the trigger and
    releases an empty version once per merge, forever. arm-dev's commit must
    re-enter, which is how a deferred release actually happens.
  * ``sync-main`` is a separate job with ``continue-on-error`` and NOTHING needs
    it — omnibase_core run 34030901325 published v0.47.4, failed on the
    main-sync app-token mint, failed the whole release job, and silently SKIPPED
    Dependency Cascade.
  * every ``uses:`` SHA-pinned — the repo's own action-pin ratchet
    (``scripts/ci/check_action_pins.py``), asserted here too so a regression is
    reported against this file by name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-on-merge.yml"
LEGACY_RELEASE_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

# PyYAML parses the bare key `on` as the boolean True (YAML 1.1), so the trigger
# block is addressed by that key, not by the string "on".
ON_KEY = True

SELF_PUSH_MARKER = "[release-on-merge]"
_SHA_PIN = re.compile(r"^[0-9a-fA-F]{40}$")
_USES = re.compile(r"""^\s*(?:-\s*)?uses:\s*["']?([^"'#\s]+)["']?""", re.MULTILINE)

POST_RELEASE_JOBS = ("reopen-dev", "sync-main")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "workflow must parse as a YAML mapping"
    return parsed


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def test_workflow_file_exists() -> None:
    assert WORKFLOW_PATH.exists(), f"workflow not found: {WORKFLOW_PATH}"


def test_triggers_only_on_a_push_to_dev(workflow: dict[str, Any]) -> None:
    triggers = workflow[ON_KEY]
    assert set(triggers) == {"push"}, (
        "release-on-merge must fire on a push to dev and nothing else; a "
        f"pull_request or schedule trigger would publish from an unmerged tree. Got {set(triggers)}"
    )
    assert triggers["push"]["branches"] == ["dev"]


def test_paths_filter_covers_exactly_the_packaged_roots(
    workflow: dict[str, Any],
) -> None:
    # SYNC with PACKAGED_PREFIXES / PACKAGED_FILES in
    # scripts/ci/decide_release_on_merge.py. The filter is the cheap pre-check;
    # the script re-derives the changed set because a push can carry several
    # commits.
    paths = workflow[ON_KEY]["push"]["paths"]
    assert set(paths) == {"src/**", "pyproject.toml", "uv.lock"}


def test_the_changed_set_is_re_derived_not_trusted_to_the_filter(raw: str) -> None:
    assert "decide_release_on_merge.py" in raw
    assert "--before" in raw
    assert "--after" in raw


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrency_is_per_repository_and_never_cancels(
    workflow: dict[str, Any],
) -> None:
    concurrency = workflow["concurrency"]
    assert "release-on-merge-" in concurrency["group"]
    assert "github.repository" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False, (
        "a cancelled release can leave a pushed tag with nothing published behind it"
    )


def test_workflow_permissions_default_to_read(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}


# ---------------------------------------------------------------------------
# Job graph
# ---------------------------------------------------------------------------


def test_the_expected_jobs_exist(workflow: dict[str, Any]) -> None:
    assert set(workflow["jobs"]) == {
        "decide",
        "release",
        "arm-dev",
        "reopen-dev",
        "sync-main",
    }


def test_release_runs_only_on_a_non_skipped_non_bumping_decision(
    workflow: dict[str, Any],
) -> None:
    condition = workflow["jobs"]["release"]["if"]
    assert "needs.decide.outputs.skip != 'true'" in condition
    assert "needs.decide.outputs.needs_bump != 'true'" in condition


def test_arm_dev_is_the_complement_of_release(workflow: dict[str, Any]) -> None:
    condition = workflow["jobs"]["arm-dev"]["if"]
    assert "needs.decide.outputs.skip != 'true'" in condition
    assert "needs.decide.outputs.needs_bump == 'true'" in condition


@pytest.mark.parametrize("job_id", POST_RELEASE_JOBS)
def test_post_release_jobs_need_release_and_never_sync_main(
    workflow: dict[str, Any], job_id: str
) -> None:
    for other_id, other in workflow["jobs"].items():
        needs = other.get("needs", [])
        needs = [needs] if isinstance(needs, str) else list(needs)
        assert "sync-main" not in needs, (
            f"job {other_id} declares `needs: sync-main`. omnibase_core run "
            "34030901325 proves that coupling: the main-sync mint failed after a "
            "successful publish and silently SKIPPED the whole downstream fan-out."
        )
    job = workflow["jobs"][job_id]
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert "release" in needs


@pytest.mark.parametrize("job_id", POST_RELEASE_JOBS)
def test_post_release_jobs_are_gated_on_the_version_output_not_job_success(
    workflow: dict[str, Any], job_id: str
) -> None:
    # The version output is set by the tag step, i.e. after the tag landed.
    # Gating on overall job success would let a later unrelated failure leave
    # dev wedged with the release-identity gate armed.
    condition = workflow["jobs"][job_id]["if"]
    assert "always()" in condition
    assert "needs.release.outputs.version != ''" in condition
    assert "needs.release.result" not in condition


def test_sync_main_cannot_fail_a_release_that_already_published(
    workflow: dict[str, Any],
) -> None:
    assert workflow["jobs"]["sync-main"]["continue-on-error"] is True


def test_sync_main_requests_the_workflows_scope_only_when_it_is_needed(
    workflow: dict[str, Any],
) -> None:
    # The onexbot-occ-writer installation does not hold Workflows:write today
    # (a standing operator item). Requesting it unconditionally is what makes
    # core's and infra's main-sync fail on every release.
    steps = workflow["jobs"]["sync-main"]["steps"]
    minting = [s for s in steps if "create-github-app-token" in str(s.get("uses", ""))]
    assert len(minting) == 2, (
        "expected one contents-only mint and one contents+workflows mint"
    )
    scoped = [s for s in minting if "permission-workflows" in s.get("with", {})]
    assert len(scoped) == 1
    assert "workflows_touched == 'true'" in scoped[0]["if"]
    unscoped = [s for s in minting if "permission-workflows" not in s.get("with", {})]
    assert "workflows_touched != 'true'" in unscoped[0]["if"]


# ---------------------------------------------------------------------------
# The anti-loop marker
# ---------------------------------------------------------------------------


def _commit_subjects(job: dict[str, Any]) -> list[str]:
    subjects: list[str] = []
    for step in job.get("steps", []):
        for line in str(step.get("run", "")).splitlines():
            stripped = line.strip()
            if stripped.startswith('git commit -m "'):
                subjects.append(stripped)
    return subjects


def test_the_reopen_commit_carries_the_self_push_marker(
    workflow: dict[str, Any],
) -> None:
    subjects = _commit_subjects(workflow["jobs"]["reopen-dev"])
    assert subjects, "reopen-dev must author a commit"
    assert all(SELF_PUSH_MARKER in subject for subject in subjects), (
        "the reopen commit touches pyproject.toml and so matches this "
        "workflow's own paths filter; without the marker its own push "
        "re-enters the trigger and releases an empty version, once per merge"
    )


def test_the_arm_dev_commit_does_not_carry_the_self_push_marker(
    workflow: dict[str, Any],
) -> None:
    subjects = _commit_subjects(workflow["jobs"]["arm-dev"])
    assert subjects, "arm-dev must author a commit"
    assert all(SELF_PUSH_MARKER not in subject for subject in subjects), (
        "arm-dev's bump MUST re-enter the workflow — that re-entry is how the "
        "deferred release actually happens"
    )


def test_the_marker_string_matches_the_decision_scripts_constant() -> None:
    from scripts.ci import decide_release_on_merge

    assert decide_release_on_merge.SELF_PUSH_MARKER == SELF_PUSH_MARKER


# ---------------------------------------------------------------------------
# Supply chain + the identity the tag is pushed as
# ---------------------------------------------------------------------------


def test_every_action_reference_is_sha_pinned(raw: str) -> None:
    unpinned: list[str] = []
    for value in _USES.findall(raw):
        if value.startswith("./"):
            continue
        action, _, ref = value.rpartition("@")
        if not action or not _SHA_PIN.match(ref):
            unpinned.append(value)
    assert not unpinned, f"unpinned action refs in release-on-merge.yml: {unpinned}"


def test_the_persisted_checkout_header_is_dropped_before_the_app_token_push(
    raw: str,
) -> None:
    # OMN-17272: actions/checkout persists the job's GITHUB_TOKEN as an
    # http.<origin>.extraheader Authorization header, and that header OVERRIDES
    # the token in the push URL — the push would authenticate as
    # github-actions[bot], which the OMN-16289 main ruleset declines.
    assert (
        'git config --local --unset-all "http.https://github.com/.extraheader"' in raw
    )
    unset_at = raw.index("--unset-all")
    push_at = raw.index('git push "https://x-access-token:')
    assert unset_at < push_at, "the extraheader must be dropped BEFORE the push"


def test_the_publish_is_idempotent_and_retries_only_transient_faults(raw: str) -> None:
    assert "--check-url https://pypi.org/simple/" in raw, (
        "the simple index ROOT, not the package-scoped URL: uv appends the "
        "package name itself, and the package-scoped form re-uploads as if no "
        "--check-url had been passed"
    )
    assert "is_retryable" in raw
    assert "non-retryable" in raw


def test_the_workflow_never_deletes_or_moves_a_tag(raw: str) -> None:
    # A bad release is followed by a FIX release. Re-tagging invalidates every
    # OCC evidence-source binding already made against that tag.
    for forbidden in (
        "git tag -d",
        "git tag -f",
        "--delete",
        "push --force",
        "-f refs/tags",
    ):
        assert forbidden not in raw, f"release-on-merge must never {forbidden!r}"


# ---------------------------------------------------------------------------
# The manual path is not regressed
# ---------------------------------------------------------------------------


def test_release_yml_still_serves_the_manual_tag_path() -> None:
    # release-on-merge is additive. release.yml remains the hand-cut and
    # re-dispatch path, and the recovery path if this workflow tags but fails to
    # publish.
    legacy = yaml.safe_load(LEGACY_RELEASE_PATH.read_text(encoding="utf-8"))
    triggers = legacy[ON_KEY]
    assert "tags" in triggers["push"]
    assert "workflow_dispatch" in triggers
