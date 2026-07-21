# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliverable 4(b): the OMN-14393 report-only workflows are structurally NON-BLOCKING.

Asserts, against the committed YAML, that both workflows are separate files whose
job ids are NOT referenced by ci.yml's required ``CI Summary`` ``needs:`` list (GHA
``needs:`` cannot cross workflow-file boundaries — so a failing attestation cannot
feed the merge-gating rollup), and that the mutate-capable author workflow defaults
to dry_run (flag OFF) and never sources its mode from PR input.

OMN-14904 (A4 activation) changes the attestation workflow's guardrail shape:

  * ``call-occ-companion-author.yml`` keeps its job-level ``continue-on-error``.
  * ``call-occ-attestation-observe.yml`` DROPS it. That job now performs a real
    durable cross-repo write (``node_occ_observation_effect`` in ``mutate``), and a
    broken write that reports green is the exact failure mode this program exists
    to remove. The report-only guarantee for the ATTESTATION result is structural,
    not exit-code based: the job carries no required-status-check name, is absent
    from ci-summary ``needs:``, and ``HandlerOccAttestationObserve.handle`` catches
    every exception and returns a fail-soft observation instead of raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_AUTHOR = _WORKFLOWS / "call-occ-companion-author.yml"
_ATTEST = _WORKFLOWS / "call-occ-attestation-observe.yml"
_CI = _WORKFLOWS / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.mark.unit
def test_author_job_is_continue_on_error() -> None:
    job = _load(_AUTHOR)["jobs"]["occ-companion-author"]
    assert job["continue-on-error"] is True, (
        "occ-companion-author must be continue-on-error to stay non-blocking"
    )


@pytest.mark.unit
def test_attestation_job_has_no_continue_on_error_swallow() -> None:
    """OMN-14904: the swallow that would hide a broken durable write is GONE.

    Regression guard against re-adding it. Non-blocking is proven structurally by
    ``test_new_jobs_are_not_in_ci_summary_needs`` /
    ``test_new_workflows_are_separate_files_not_folded_into_ci``, not by swallowing
    exit codes.
    """
    data = _load(_ATTEST)
    job = data["jobs"]["occ-attestation-observe"]
    assert "continue-on-error" not in job, (
        "occ-attestation-observe must NOT carry a job-level continue-on-error: it "
        "performs a real durable cross-repo write and a broken write must be RED"
    )
    for step in job["steps"]:
        assert "continue-on-error" not in step, (
            f"step {step.get('name')!r} must not re-introduce the swallow"
        )


@pytest.mark.unit
def test_new_jobs_are_not_in_ci_summary_needs() -> None:
    ci = _load(_CI)
    needs = ci["jobs"]["ci-summary"]["needs"]
    for forbidden in ("occ-companion-author", "occ-attestation-observe"):
        assert forbidden not in needs, (
            f"{forbidden} must NOT gate the required CI Summary rollup"
        )


@pytest.mark.unit
def test_new_workflows_are_separate_files_not_folded_into_ci() -> None:
    # Being in their own workflow files is what makes them un-addable to
    # ci-summary's needs (cross-file needs are impossible in GHA).
    ci = _load(_CI)
    assert "occ-companion-author" not in ci["jobs"]
    assert "occ-attestation-observe" not in ci["jobs"]


@pytest.mark.unit
def test_author_workflow_defaults_to_dry_run_and_never_uses_pr_input() -> None:
    text = _AUTHOR.read_text(encoding="utf-8")
    # Default-off: the mode resolves from the operator repo variable, defaulting
    # to dry_run via a fail-closed allowlist.
    assert 'MODE="${OMNI_OCC_AUTOAUTHOR_MODE:-dry_run}"' in text
    assert "dry_run|mutate)" in text  # allowlist branch
    assert "OMNI_OCC_AUTOAUTHOR_MODE: ${{ vars.OMNI_OCC_AUTOAUTHOR_MODE }}" in text
    payload_step = text.split("Build author payload", 1)[1].split(
        "Run node_occ_companion_effect", 1
    )[0]
    # Injection-safety: mode is NEVER taken from PR title/body/head_ref.
    for forbidden in (
        "github.event.pull_request.title",
        "github.event.pull_request.body",
        "github.event.pull_request.head.ref",
    ):
        assert forbidden not in payload_step
    # The payload's mode comes from the validated shell var, not a PR field.
    assert '--arg mode "$MODE"' in payload_step


@pytest.mark.unit
def test_author_workflow_uses_occ_write_token_only_for_mutate() -> None:
    text = _AUTHOR.read_text(encoding="utf-8")
    assert "Validate mutate write token" in text
    assert "OCC_AUTOAUTHOR_TOKEN: ${{ secrets.OCC_AUTOAUTHOR_TOKEN }}" in text
    assert 'if [ "$MODE" = "mutate" ]; then' in text
    assert (
        'export GITHUB_TOKEN="${OCC_AUTOAUTHOR_TOKEN:?mutate requires OCC_AUTOAUTHOR_TOKEN}"'
        in text
    )


@pytest.mark.unit
def test_new_workflows_pin_upload_artifact_action() -> None:
    pinned = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    assert pinned in _AUTHOR.read_text(encoding="utf-8")
    assert pinned in _ATTEST.read_text(encoding="utf-8")


@pytest.mark.unit
def test_attestation_workflow_has_no_write_permissions() -> None:
    """`permissions:` stays READ-ONLY even after the OMN-14904 mutate activation.

    Raising it would not enable the write and would be a pure over-grant:
    ``permissions:`` scopes only the ambient per-run ``GITHUB_TOKEN``, which is
    repo-scoped to ``omnimarket`` and can never push to ``onex_change_control`` at
    ANY permission level. The cross-repo write uses a separate narrow credential
    (``secrets.OCC_AUTOAUTHOR_TOKEN``), asserted below.
    """
    data = _load(_ATTEST)
    perms = data["permissions"]
    assert perms.get("contents") == "read"
    assert perms.get("pull-requests") == "read"
    assert "write" not in set(perms.values())


# --------------------------------------------------------------------------- #
# OMN-14904 — A4 activation of the durable observation store                    #
# --------------------------------------------------------------------------- #


def _attest_step(name_fragment: str) -> dict[str, Any]:
    steps = _load(_ATTEST)["jobs"]["occ-attestation-observe"]["steps"]
    matches = [s for s in steps if name_fragment in str(s.get("name", ""))]
    assert len(matches) == 1, (
        f"expected exactly one step whose name contains {name_fragment!r}, "
        f"found {[s.get('name') for s in matches]}"
    )
    return cast("dict[str, Any]", matches[0])


@pytest.mark.unit
def test_observation_store_defaults_to_mutate_with_a_fail_closed_allowlist() -> None:
    """Activation is the COMMITTED DEFAULT; the operator variable is a kill switch."""
    step = _attest_step("Resolve observation-store write mode")
    script = step["run"]
    # Default is mutate (LIVE) when the operator variable is unset.
    assert 'MODE="${OMNI_OCC_OBSERVATION_STORE_MODE:-mutate}"' in script
    # Fail-closed allowlist: an unrecognized value degrades to dry_run, never mutate.
    assert "dry_run|mutate)" in script
    assert 'MODE="dry_run"' in script
    # Sourced from the operator repo variable, NEVER from PR-controlled input.
    assert (
        step["env"]["OMNI_OCC_OBSERVATION_STORE_MODE"]
        == "${{ vars.OMNI_OCC_OBSERVATION_STORE_MODE }}"
    )
    for forbidden in (
        "github.event.pull_request.title",
        "github.event.pull_request.body",
        "github.event.pull_request.head.ref",
    ):
        assert forbidden not in str(step)


@pytest.mark.unit
def test_observation_store_has_an_explicit_fork_guard() -> None:
    """No cross-repo write may ever be attempted from a fork head."""
    job = _load(_ATTEST)["jobs"]["occ-attestation-observe"]
    # Primary guard: the job does not even run for a fork head.
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in job["if"]
    )
    # Belt-and-braces guard inside the mode resolver.
    step = _attest_step("Resolve observation-store write mode")
    assert (
        step["env"]["HEAD_REPO"]
        == "${{ github.event.pull_request.head.repo.full_name }}"
    )
    assert step["env"]["THIS_REPO"] == "${{ github.repository }}"
    assert 'if [ "$HEAD_REPO" != "$THIS_REPO" ]; then' in step["run"]
    # The fork branch forces dry_run — it must not fall through to mutate.
    fork_branch = step["run"].split('if [ "$HEAD_REPO" != "$THIS_REPO" ]; then', 1)[1]
    assert 'MODE="dry_run"' in fork_branch.split("fi", 1)[0]


@pytest.mark.unit
def test_observation_store_wires_the_narrow_cross_repo_write_token() -> None:
    """The write credential is passed explicitly and validated fail-LOUD."""
    validate = _attest_step("Validate OCC cross-repo write token")
    assert validate["if"] == "${{ steps.obsmode.outputs.mode == 'mutate' }}"
    assert (
        validate["env"]["OCC_AUTOAUTHOR_TOKEN"] == "${{ secrets.OCC_AUTOAUTHOR_TOKEN }}"
    )
    # Absent secret must FAIL the job, not silently degrade to dry_run.
    assert 'if [ -z "${OCC_AUTOAUTHOR_TOKEN:-}" ]; then' in validate["run"]
    assert "::error::" in validate["run"]
    assert "exit 1" in validate["run"]

    write = _attest_step("Run node_occ_observation_effect")
    assert write["env"]["OCC_AUTOAUTHOR_TOKEN"] == "${{ secrets.OCC_AUTOAUTHOR_TOKEN }}"
    assert write["env"]["OBS_MODE"] == "${{ steps.obsmode.outputs.mode }}"
    assert 'if [ "$OBS_MODE" = "mutate" ]; then' in write["run"]
    assert (
        'export GITHUB_TOKEN="${OCC_AUTOAUTHOR_TOKEN:?mode=mutate requires OCC_AUTOAUTHOR_TOKEN}"'
        in write["run"]
    )


@pytest.mark.unit
def test_observation_store_rejects_the_org_wide_pat() -> None:
    """Rolling plan §2 A3: the org-wide CROSS_REPO_PAT is explicitly not used."""
    assert "secrets.CROSS_REPO_PAT" not in _ATTEST.read_text(encoding="utf-8")


@pytest.mark.unit
def test_observation_store_steps_fail_loud_on_absent_input() -> None:
    """An absent input must FAIL — a check that silently skips does not exist."""
    for fragment in (
        "Build node_occ_observation_effect payload",
        "Run node_occ_observation_effect",
    ):
        script = _attest_step(fragment)["run"]
        assert "set -euo pipefail" in script
        assert "::error::" in script
        assert "exit 1" in script
        # The pre-OMN-14904 swallows: a `|| echo ...` fallback, and an
        # `if [ -s <input> ]; then ... else echo "skipping" fi` guard whose
        # else-branch let an absent input pass green.
        assert "|| echo" not in script
        assert "; skipping" not in script
        assert "if [ ! -s" in script, (
            "the absent-input branch must be the FAILURE branch, not an else-skip"
        )


@pytest.mark.unit
def test_observation_store_mode_flows_into_the_effect_payload() -> None:
    """One source of truth: the resolved mode reaches the node, not a hardcode."""
    build = _attest_step("Build node_occ_observation_effect payload")
    assert build["env"]["OBS_MODE"] == "${{ steps.obsmode.outputs.mode }}"
    assert '--mode "$OBS_MODE"' in build["run"]
    # The old hardcoded dry_run wiring is gone from the whole workflow.
    assert "--mode dry_run" not in _ATTEST.read_text(encoding="utf-8")
