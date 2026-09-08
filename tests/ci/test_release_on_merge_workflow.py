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
# block is addressed by that key, not by the string "on". Typed `Any` because a
# `dict[str, Any]` subscripted by a `bool` is a mypy --strict index error, and
# the alternative (re-typing every workflow mapping as `dict[Any, Any]`) would
# lose the key typing everywhere else in this module for one lookup.
ON_KEY: Any = True

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


def _executable_lines(raw: str) -> str:
    """The workflow with every comment line removed.

    Both of the assertions below are about what the workflow DOES, and this
    file's own prose has to be free to name the broken shape it is pinning
    against — the comment in `release-on-merge.yml` that records why the
    `--unset-all` shape was removed would otherwise trip the very test that
    forbids it. Dropping lines whose first non-space character is `#` covers
    YAML comments and the shell comments inside `run:` blocks alike; neither
    class executes.
    """
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )


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


def test_the_release_job_can_actually_create_a_github_release(
    workflow: dict[str, Any],
) -> None:
    """`softprops/action-gh-release` runs as GITHUB_TOKEN and needs contents:write.

    Measured in run 34066508864 (v0.4.20, 2026-09-06T23:30:26Z): under the
    workflow-level `contents: read`, the step returned

        GitHub release failed with status: 403
        {"message": "Resource not accessible by integration"}

    and it did so AFTER the tag was pushed and 0.4.20 was already live on PyPI —
    i.e. the release was real and only its GitHub-side record was missing. A
    job-level `permissions:` block REPLACES the workflow-level one, so the scope
    has to be declared on this job specifically.
    """
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}


def test_only_the_release_job_elevates_the_workflow_token(
    workflow: dict[str, Any],
) -> None:
    """Every other job keeps the workflow-level read-only GITHUB_TOKEN.

    The pushing jobs (`arm-dev`, `reopen-dev`) and `sync-main` authenticate as
    the minted App token, never as GITHUB_TOKEN, so an elevation on any of them
    would be scope with no consumer.
    """
    elevated = {
        job_id
        for job_id, job in workflow["jobs"].items()
        if job.get("permissions") is not None
    }
    assert elevated == {"release"}, f"unexpected job-level permissions: {elevated}"


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


def test_no_job_tries_to_strip_a_persisted_checkout_header(raw: str) -> None:
    """The `--unset-all extraheader` shape is a NO-OP under actions/checkout v7.

    Measured live, run 34059788335 (2026-09-06T21:02:43Z, release of 0.4.19):
    checkout v7 does not write the Authorization header into ``.git/config`` at
    all. It writes it to a generated credentials file under the runner temp
    directory and points git at it with ``git config --file <that file>
    http.https://github.com/.extraheader ...`` — so ``git config --local
    --unset-all http.https://github.com/.extraheader`` removes nothing, the
    header still wins over any URL-embedded credential, and the tag push failed
    with ``remote: Permission to OmniNode-ai/omnimarket.git denied to
    github-actions[bot].``

    The shape is worse than useless: it reads as protection while protecting
    nothing, and the failure only surfaces on a real release. Pinned so it
    cannot come back.
    """
    assert "--unset-all" not in _executable_lines(raw), (
        "stripping the persisted checkout credential is a no-op under "
        "actions/checkout v7 — hand the App token to checkout as `token:` "
        "instead and push to plain `origin`"
    )


def test_no_push_embeds_a_token_in_the_remote_url(raw: str) -> None:
    """Every push authenticates through the checkout credential, not a URL.

    A URL-embedded token loses to the persisted header (see the test above), so
    the only credential that can actually be in force is the one checkout was
    given. Keeping that single-sourced is what makes the identity of a push
    readable from the checkout step rather than from a shell line.
    """
    executable = _executable_lines(raw)
    assert "x-access-token:" not in executable
    assert "@github.com/${GITHUB_REPOSITORY}" not in executable


@pytest.mark.parametrize("job_id", ["release", "arm-dev", "reopen-dev"])
def test_every_pushing_job_checks_out_with_the_app_token(
    workflow: dict[str, Any], job_id: str
) -> None:
    """The App token is minted BEFORE the checkout and handed to it.

    `release` pushes the tag, `arm-dev` and `reopen-dev` push a branch (and
    `reopen-dev` attempts dev itself). All three must carry the App identity,
    and the only shape that survives checkout v7 is `token:` on the checkout.
    Order matters: a mint placed after the checkout cannot influence it.
    """
    steps = workflow["jobs"][job_id]["steps"]
    mint_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
    )
    checkout_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert mint_index < checkout_index, (
        f"{job_id}: the App token must be minted before the checkout it feeds"
    )
    token = steps[checkout_index].get("with", {}).get("token", "")
    assert "steps.app-token.outputs.token" in token, (
        f"{job_id}: checkout must be given the App token, got {token!r}"
    )


def test_the_release_job_pushes_the_tag_to_plain_origin(raw: str) -> None:
    assert 'git push origin "refs/tags/${TAG}"' in raw


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


def test_the_manual_release_path_publishes_idempotently() -> None:
    """release.yml's publish must tolerate files that are already on PyPI.

    The design assumed the App-token tag push from release-on-merge would be
    suppressed the way App-token branch pushes are on this org. Measured false:
    the v0.4.20 tag push fired release.yml as run 34067071382 while
    release-on-merge run 34066508864 was still publishing the same two files.
    Two `uv publish` calls for one version overlapped and both reported
    success, which is luck rather than a property. `--check-url` against the
    simple index ROOT makes the upload idempotent, so the loser of that race
    skips instead of taking a 400 and reporting a red release for an artifact
    that is already live.
    """
    raw_legacy = LEGACY_RELEASE_PATH.read_text(encoding="utf-8")
    assert "--check-url https://pypi.org/simple/" in raw_legacy


# ---------------------------------------------------------------------------
# The two bump-PR producers must not be blind to each other (OMN-18010).
# ---------------------------------------------------------------------------

ARM_DEV_BRANCH_PREFIX = "automation/omn-18010-arm-dev-"
REOPEN_BRANCH_PREFIX = "automation/omn-18010-post-release-dev-bump-"
LEGACY_ARM_DEV_BRANCH_PREFIX = "automation/arm-dev-"
LEGACY_REOPEN_BRANCH_PREFIX = "automation/post-release-dev-bump-"


def _job_shell(workflow: dict[str, Any], job_id: str) -> str:
    """Every ``run:`` script in a job, concatenated."""
    return "\n".join(
        step["run"] for step in workflow["jobs"][job_id]["steps"] if "run" in step
    )


@pytest.mark.parametrize("job_id", ["arm-dev", "reopen-dev"])
def test_a_bump_pr_guard_looks_for_the_peer_jobs_branch_too(
    workflow: dict[str, Any], job_id: str
) -> None:
    """Both bump producers must guard on the target VERSION, not their own branch.

    ``arm-dev`` pushes ``automation/omn-18010-arm-dev-<v>``; ``reopen-dev`` pushes
    ``automation/omn-18010-post-release-dev-bump-<v>``. Both open a PR that sets
    ``[project].version`` to the same ``<v>``, and each guarded only on its OWN
    branch name -- so the two were invisible to each other.

    Measured live: release run 34066508864 published v0.4.20 and its
    ``reopen-dev`` opened #2361 (0.4.20 -> 0.4.21). The next source merge landed
    at 23:39Z before #2361 had merged, so dev's version was still level with the
    v0.4.20 tag; run 34067512248 decided ``needs_bump`` and its ``arm-dev``
    opened #2364 -- a byte-identical 0.4.20 -> 0.4.21 bump, also armed for
    auto-merge. Whichever lands first, the other can never merge: its diff
    context (``version = "0.4.20"``) no longer exists, so it is left permanently
    CONFLICTING with auto-merge still armed.

    That window is not rare -- it is open on every release until the reopen PR
    merges, which is precisely when merges are most likely to be arriving.
    """
    shell = _job_shell(workflow, job_id)
    for prefix in (
        ARM_DEV_BRANCH_PREFIX,
        REOPEN_BRANCH_PREFIX,
        LEGACY_ARM_DEV_BRANCH_PREFIX,
        LEGACY_REOPEN_BRANCH_PREFIX,
    ):
        assert prefix in shell, (
            f"{job_id} does not consider {prefix!r} when checking for an "
            f"in-flight bump PR, so it can open a duplicate of the peer job's."
        )


def test_generated_bump_pr_branches_bind_the_release_ticket(
    workflow: dict[str, Any],
) -> None:
    """Bump PR bodies cite OMN-18010, so generated branches must bind that ticket."""
    for job_id, prefix in (
        ("arm-dev", ARM_DEV_BRANCH_PREFIX),
        ("reopen-dev", REOPEN_BRANCH_PREFIX),
    ):
        shell = _job_shell(workflow, job_id)
        assert f"branch={prefix}${{TARGET}}" in shell


# ---------------------------------------------------------------------------
# The pin-resolvability gate and its ORDER (OMN-18034).
#
# A step that exists but runs in the wrong place is the failure this section
# guards, and it is invisible in review: moving one YAML block down by three
# entries still reads as "the gate is wired up".
# ---------------------------------------------------------------------------

PIN_GATE_SCRIPT = "scripts/ci/verify_pypi_pin_resolvability.py"


def _step_names(workflow: dict[str, Any], job_id: str) -> list[str]:
    """Every named step of a job, in file order."""
    return [
        str(step["name"])
        for step in workflow["jobs"][job_id]["steps"]
        if step.get("name")
    ]


def _index_of_step_running(workflow: dict[str, Any], job_id: str, needle: str) -> int:
    """Index of the first step whose ``run:`` mentions ``needle``."""
    steps = workflow["jobs"][job_id]["steps"]
    for i, step in enumerate(steps):
        if needle in str(step.get("run", "")):
            return i
    raise AssertionError(
        f"no step in job {job_id!r} runs {needle!r}; steps are "
        f"{_step_names(workflow, job_id)}"
    )


def test_the_pin_gate_script_is_vendored() -> None:
    """The workflows invoke it by path; an absent file is a red release, not a skip."""
    assert (REPO_ROOT / PIN_GATE_SCRIPT).is_file(), (
        f"{PIN_GATE_SCRIPT} is missing, so both release paths would fail at the "
        "gate step with a file-not-found rather than a pin verdict."
    )


def test_release_on_merge_verifies_pins_before_it_tags(
    workflow: dict[str, Any],
) -> None:
    """Build -> verify dist -> RESOLVE -> tag -> publish, in that order.

    Gating only the publish would still push the tag first, leaving a version
    that looks released, has no artifact, and can never be re-cut at that
    number. A red gate has to be a complete no-op, and it only is if nothing
    irreversible has happened yet.
    """
    gate = _index_of_step_running(workflow, "release", PIN_GATE_SCRIPT)
    build = _index_of_step_running(workflow, "release", "uv build")
    tag = _index_of_step_running(workflow, "release", "git push origin")
    publish = _index_of_step_running(workflow, "release", "uv publish")

    assert build < gate, "the gate needs the wheel `uv build` produces"
    assert gate < tag, (
        "the pin gate runs AFTER the tag push, so an unresolvable release "
        "would still leave a tag behind. Order must be build -> gate -> tag."
    )
    assert tag < publish


def test_release_yml_verifies_pins_before_it_publishes(
    workflow: dict[str, Any],
) -> None:
    """The manual/tag path is not a hole in the gate.

    It is not dormant either: the v0.4.20 tag push fired release.yml as run
    34067071382 while release-on-merge was publishing the same files. A gate on
    one of two publishing paths is a coin flip.
    """
    legacy = yaml.safe_load(LEGACY_RELEASE_PATH.read_text(encoding="utf-8"))
    gate = _index_of_step_running(legacy, "release", PIN_GATE_SCRIPT)
    build = _index_of_step_running(legacy, "release", "uv build")
    publish = _index_of_step_running(legacy, "release", "uv publish")

    assert build < gate < publish, (
        "release.yml must resolve the built wheel's pins after building it and "
        "before publishing it."
    )


@pytest.mark.parametrize(
    ("path", "label"),
    [(WORKFLOW_PATH, "release-on-merge.yml"), (LEGACY_RELEASE_PATH, "release.yml")],
)
def test_the_pin_gate_budget_is_set_explicitly(path: Path, label: str) -> None:
    """The budget is a measurement, not an inherited default.

    The script's built-in default (1800s) is shaped by omnibase_infra's
    self-hosted fleet. omnimarket releases on ubuntu-latest and its closure is
    178 packages / 345 MB (measured 2026-09-07 against omnimarket==0.4.20, the
    last resolvable release). Setting it here keeps the number reviewable
    alongside the job's own timeout-minutes instead of hidden in a vendored
    file.
    """
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["release"]["steps"]
    gate = next(s for s in steps if PIN_GATE_SCRIPT in str(s.get("run", "")))
    budget = str(gate.get("env", {}).get("PYPI_PIN_RESOLVE_TIMEOUT_SECONDS", ""))
    assert budget.isdigit(), (
        f"{label} does not set PYPI_PIN_RESOLVE_TIMEOUT_SECONDS on the pin gate"
    )
    ceiling_minutes = parsed["jobs"]["release"].get("timeout-minutes")
    if ceiling_minutes is not None:
        assert int(budget) < int(ceiling_minutes) * 60, (
            f"{label}: the gate's budget ({budget}s) meets or exceeds the "
            f"release job's own ceiling ({ceiling_minutes}m), so a slow install "
            "would be killed as an opaque job timeout instead of reported as "
            "this script's THROUGHPUT diagnostic (OMN-16047)."
        )
