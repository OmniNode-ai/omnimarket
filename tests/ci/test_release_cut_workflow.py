# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural proof for .github/workflows/release-cut.yml (OMN-18010).

A release workflow is the one workflow whose defects are hardest to observe: it
does not run on pull requests, so a mistake in it is invisible until a real cut
either publishes the wrong thing or silently publishes nothing. The unit tests
in tests/unit/scripts/test_decide_release_cut.py pin the DECISION; this module
pins the parts of the wiring that no unit test can reach — the trigger, the
concurrency contract, the job graph, and the pin ratchet.

release-cut.yml replaced release-on-merge.yml (OMN-18010): it fires only on
workflow_dispatch, never on a push to dev, because the operator ruled releases
happen when something is ready to use, not on every merge. Every assertion here
corresponds to a measured failure carried over from the retired workflow, or to
the explicit-trigger property that replaced its per-merge one:

  * ``cancel-in-progress: false`` — a cancelled release can leave a pushed tag
    with nothing published behind it.
  * the trigger is `workflow_dispatch` ONLY — a `push` trigger here is exactly
    the regression this ticket exists to prevent.
  * a not-ready decision FAILS the run rather than opening a bump PR — the
    retired workflow's `arm-dev` job existed only to make the per-merge race
    safe, and re-adding an automatic bump PR here would reintroduce the noise
    the operator ruled out (13 of 44 omnimarket PRs on 2026-09-27 were bump
    PRs).
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
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-cut.yml"
LEGACY_RELEASE_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

# PyYAML parses the bare key `on` as the boolean True (YAML 1.1), so the trigger
# block is addressed by that key, not by the string "on".
ON_KEY: Any = True

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


def _executable_lines(raw: str) -> str:
    """The workflow with every comment line removed (neither YAML nor shell
    comments execute, and this file's prose has to be free to name the shapes
    it forbids without tripping the very test that forbids them)."""
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Trigger — the whole point of this ticket
# ---------------------------------------------------------------------------


def test_workflow_file_exists() -> None:
    assert WORKFLOW_PATH.exists(), f"workflow not found: {WORKFLOW_PATH}"


def test_the_retired_per_merge_workflow_is_actually_gone() -> None:
    retired = REPO_ROOT / ".github" / "workflows" / "release-on-merge.yml"
    assert not retired.exists(), (
        "release-on-merge.yml still exists alongside release-cut.yml — a "
        "repository cannot have two workflows that can both publish the same "
        "release, and this repo's App-token tag push does not reliably "
        "suppress the other one firing on the same push."
    )


def test_triggers_only_on_workflow_dispatch(workflow: dict[str, Any]) -> None:
    triggers = workflow[ON_KEY]
    assert set(triggers) == {"workflow_dispatch"}, (
        "release-cut must fire ONLY on an explicit human (or skill-issued) "
        "dispatch. A push, pull_request or schedule trigger here is exactly "
        "the automatic-release-on-merge regression this ticket retired. Got "
        f"{set(triggers)}"
    )


def test_the_decision_script_takes_no_push_range(raw: str) -> None:
    # decide_release_cut.py reads the checked-out tree directly; there is no
    # before/after push range to re-derive a changed set from, because there is
    # no push trigger to have carried one.
    assert "decide_release_cut.py" in raw
    assert "--before" not in raw
    assert "--after" not in raw


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrency_is_per_repository_and_never_cancels(
    workflow: dict[str, Any],
) -> None:
    concurrency = workflow["concurrency"]
    assert "release-cut-" in concurrency["group"]
    assert "github.repository" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False, (
        "a cancelled release can leave a pushed tag with nothing published behind it"
    )


def test_the_release_job_can_actually_create_a_github_release(
    workflow: dict[str, Any],
) -> None:
    """`softprops/action-gh-release` runs as GITHUB_TOKEN and needs contents:write.

    Measured in run 34066508864 (v0.4.20, 2026-09-06T23:30:26Z): under the
    workflow-level `contents: read`, the step returned a 403 AFTER the tag was
    pushed and the wheel was already live on PyPI. A job-level `permissions:`
    block REPLACES the workflow-level one, so the scope has to be declared on
    this job specifically.
    """
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}


def test_only_the_release_job_elevates_the_workflow_token(
    workflow: dict[str, Any],
) -> None:
    """Every other job keeps the workflow-level read-only GITHUB_TOKEN.

    The pushing jobs (`reopen-dev`) and `sync-main` authenticate as the minted
    App token, never as GITHUB_TOKEN, so an elevation on any of them would be
    scope with no consumer.
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
        "reopen-dev",
        "sync-main",
    }


def test_there_is_no_arm_dev_job(workflow: dict[str, Any]) -> None:
    # arm-dev existed only to recover from the race where a merge landed while
    # dev sat level with the just-published tag — a symptom of releasing on
    # every merge. With releases cut explicitly, at most one cut runs at a time
    # (see the concurrency test above), so there is nothing to recover from,
    # and a not-ready decision fails the run instead of opening a bump PR.
    assert "arm-dev" not in workflow["jobs"]


def test_a_not_ready_decision_fails_the_run_loudly(workflow: dict[str, Any]) -> None:
    decide_steps = workflow["jobs"]["decide"]["steps"]
    fail_step = next(
        s
        for s in decide_steps
        if s.get("name") == "Fail loudly when dev is not release-ready"
    )
    assert fail_step["if"] == "steps.decide.outputs.ready != 'true'"
    assert "::error::" in fail_step["run"]
    assert "exit 1" in fail_step["run"]


def test_release_runs_only_when_the_decision_is_ready(
    workflow: dict[str, Any],
) -> None:
    condition = workflow["jobs"]["release"]["if"]
    assert condition == "needs.decide.outputs.ready == 'true'"


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
# Every checkout that will tag or push runs against dev explicitly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["decide", "release", "reopen-dev"])
def test_every_checkout_pins_ref_dev_explicitly(
    workflow: dict[str, Any], job_id: str
) -> None:
    """workflow_dispatch checks out whatever ref it was dispatched against,
    defaulting to the repository's default branch — which is NOT dev. Every
    job that decides or releases must say `ref: dev` rather than trust the
    dispatch context."""
    steps = workflow["jobs"][job_id]["steps"]
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout.get("with", {}).get("ref") == "dev"


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
    assert not unpinned, f"unpinned action refs in release-cut.yml: {unpinned}"


def test_no_job_tries_to_strip_a_persisted_checkout_header(raw: str) -> None:
    """The `--unset-all extraheader` shape is a NO-OP under actions/checkout v7
    (measured live, run 34059788335, release of 0.4.19). Hand the App token to
    checkout as `token:` instead and push to plain `origin`."""
    assert "--unset-all" not in _executable_lines(raw), (
        "stripping the persisted checkout credential is a no-op under "
        "actions/checkout v7 — hand the App token to checkout as `token:` "
        "instead and push to plain `origin`"
    )


def test_no_push_embeds_a_token_in_the_remote_url(raw: str) -> None:
    """Every push authenticates through the checkout credential, not a URL.

    A URL-embedded token loses to the persisted header (see the test above), so
    the only credential that can actually be in force is the one checkout was
    given.
    """
    executable = _executable_lines(raw)
    assert "x-access-token:" not in executable
    assert "@github.com/${GITHUB_REPOSITORY}" not in executable


@pytest.mark.parametrize("job_id", ["release", "reopen-dev"])
def test_every_pushing_job_checks_out_with_the_app_token(
    workflow: dict[str, Any], job_id: str
) -> None:
    """The App token is minted BEFORE the checkout and handed to it.

    `release` pushes the tag, `reopen-dev` pushes a branch (and attempts dev
    itself). Both must carry the App identity, and the only shape that
    survives checkout v7 is `token:` on the checkout. Order matters: a mint
    placed after the checkout cannot influence it.
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
        assert forbidden not in raw, f"release-cut must never {forbidden!r}"


# ---------------------------------------------------------------------------
# The manual/recovery path is not regressed
# ---------------------------------------------------------------------------


def test_release_yml_still_serves_the_manual_tag_path() -> None:
    # release-cut.yml is additive on top of release.yml, which remains the
    # recovery path if release-cut tags but fails to publish.
    legacy = yaml.safe_load(LEGACY_RELEASE_PATH.read_text(encoding="utf-8"))
    triggers = legacy[ON_KEY]
    assert "tags" in triggers["push"]
    assert "workflow_dispatch" in triggers


def test_the_manual_release_path_publishes_idempotently() -> None:
    """release.yml's publish must tolerate files that are already on PyPI.

    `--check-url` against the simple index ROOT makes the upload idempotent, so
    a race between this path and release-cut.yml's tag push (however unlikely
    now that releases are explicit and serialized) still cannot double-publish.
    """
    raw_legacy = LEGACY_RELEASE_PATH.read_text(encoding="utf-8")
    assert "--check-url https://pypi.org/simple/" in raw_legacy


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


def test_release_cut_verifies_pins_before_it_tags(
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
    """The manual/tag recovery path is not a hole in the gate."""
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
    [(WORKFLOW_PATH, "release-cut.yml"), (LEGACY_RELEASE_PATH, "release.yml")],
)
def test_the_pin_gate_budget_is_set_explicitly(path: Path, label: str) -> None:
    """The budget is a measurement, not an inherited default.

    The script's built-in default (1800s) is shaped by omnibase_infra's
    self-hosted fleet. omnimarket releases on ubuntu-latest and its closure is
    178 packages / 345 MB (measured 2026-09-07 against omnimarket==0.4.20).
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
