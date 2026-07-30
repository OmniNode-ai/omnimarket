# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliverable 4(b): the OMN-14393 report-only workflows are structurally NON-BLOCKING.

Asserts, against the committed YAML, that both workflows are separate files whose
job ids are NOT part of the required ``CI Summary`` poller's own workflow jobs or
gate anchors (GHA jobs cannot cross workflow-file boundaries), and that the
mutate-capable author workflow defaults to dry_run (flag OFF) and never sources
its mode from PR input.

OMN-14904 (A4 activation) changes the attestation workflow's guardrail shape:

  * ``call-occ-companion-author.yml`` keeps its job-level ``continue-on-error``.
  * ``call-occ-attestation-observe.yml`` DROPS it. That job now performs a real
    durable cross-repo write (``node_occ_observation_effect`` in ``mutate``), and a
    broken write that reports green is the exact failure mode this program exists
    to remove. The report-only guarantee for the ATTESTATION result is structural,
    not exit-code based: the job carries no required-status-check name, is in a
    separate workflow file that the no-``needs`` CI Summary poller cannot observe,
    is absent from ``scripts/ci/ci_summary_gate.py`` gate anchors, and
    ``HandlerOccAttestationObserve.handle`` catches every exception and returns a
    fail-soft observation instead of raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci.ci_summary_gate import SKIPPABLE_GATE_JOBS, STRICT_GATE_JOBS

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
def test_new_jobs_do_not_gate_ci_summary_poller() -> None:
    ci = _load(_CI)
    summary = ci["jobs"]["ci-summary"]
    # OMN-14127 (CI-G2): ci-summary is a NO-`needs` fail-closed poller. It polls
    # only its OWN workflow run's jobs, so cross-file report-only OCC jobs can
    # never be observed — let alone gate — the required CI Summary context.
    assert "needs" not in summary, (
        "ci-summary must be a NO-`needs` poller (OMN-14127); a needs-gated "
        "required context wedges the PR under fleet saturation."
    )
    gate_anchors = set(STRICT_GATE_JOBS) | set(SKIPPABLE_GATE_JOBS)
    for forbidden in ("occ-companion-author", "occ-attestation-observe"):
        # Separate workflow files — not folded into ci.yml (cross-file, so the
        # poller never even sees them) and not among its gate anchors.
        assert forbidden not in ci["jobs"], (
            f"{forbidden} must stay in its own workflow file, not ci.yml"
        )
        assert forbidden not in gate_anchors, (
            f"{forbidden} must NOT be a CI Summary poller gate anchor"
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


def _author_step(name_fragment: str) -> dict[str, Any]:
    steps = _load(_AUTHOR)["jobs"]["occ-companion-author"]["steps"]
    matches = [s for s in steps if name_fragment in str(s.get("name", ""))]
    assert len(matches) == 1, (
        f"expected exactly one step whose name contains {name_fragment!r}, "
        f"found {[s.get('name') for s in matches]}"
    )
    return cast("dict[str, Any]", matches[0])


@pytest.mark.unit
def test_author_workflow_uses_occ_write_token_only_for_mutate() -> None:
    """The Toggle-1 write credential is minted per-run, scoped to one repo, fail-LOUD.

    OMN-15350: the OMN-14904 ``secrets.OCC_AUTOAUTHOR_TOKEN`` PAT was never
    provisioned (live readback 2026-07-29: absent from both the org and the
    omnimarket repo secret sets), so this mutate path was credential-dead on
    arrival. The credential is now the least-privilege OnexBot-OCC-Writer
    GitHub App (org secrets ``ONEXBOT_OCC_APP_ID``/``ONEXBOT_OCC_PRIVATE_KEY``),
    minted via the same canonical ``actions/create-github-app-token`` pattern
    ``call-occ-attestation-observe.yml`` (OMN-14955) and ``pr-arch-review.yml``
    use, and scoped down to ``OmniNode-ai/onex_change_control`` only.
    """
    mutate_only = "${{ vars.OMNI_OCC_AUTOAUTHOR_MODE == 'mutate' }}"

    validate = _author_step("Validate OCC cross-repo write credential")
    assert validate["if"] == mutate_only
    assert validate["env"]["ONEXBOT_OCC_APP_ID"] == "${{ secrets.ONEXBOT_OCC_APP_ID }}"
    assert (
        validate["env"]["ONEXBOT_OCC_PRIVATE_KEY"]
        == "${{ secrets.ONEXBOT_OCC_PRIVATE_KEY }}"
    )
    # Absent secrets must FAIL the job, not silently degrade to dry_run.
    assert (
        'if [ -z "${ONEXBOT_OCC_APP_ID:-}" ] || [ -z "${ONEXBOT_OCC_PRIVATE_KEY:-}" ]; then'
        in validate["run"]
    )
    assert "::error::" in validate["run"]
    assert "exit 1" in validate["run"]

    mint = _author_step("Mint OCC write token")
    assert mint["id"] == "occ-app-token"
    assert mint["if"] == mutate_only
    assert str(mint["uses"]).startswith("actions/create-github-app-token@")
    assert mint["with"]["app-id"] == "${{ secrets.ONEXBOT_OCC_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.ONEXBOT_OCC_PRIVATE_KEY }}"
    # Scoped-down installation token: one org, ONE repository. Without these
    # `with:` keys the token would cover every repo the App is installed on.
    assert mint["with"]["owner"] == "OmniNode-ai"
    assert mint["with"]["repositories"] == "onex_change_control"

    run = _author_step("Run node_occ_companion_effect")
    assert run["env"]["OCC_WRITE_TOKEN"] == "${{ steps.occ-app-token.outputs.token }}"
    assert 'if [ "$MODE" = "mutate" ]; then' in run["run"]
    assert (
        'export GITHUB_TOKEN="${OCC_WRITE_TOKEN:?mode=mutate requires the minted OnexBot-OCC-Writer token}"'
        in run["run"]
    )


@pytest.mark.unit
def test_author_workflow_mints_a_separate_product_repo_token() -> None:
    """OMN-15441: the product-body stamp gets its OWN product-scoped credential.

    The node writes into TWO repos: onex_change_control (lease/clone/push/PR)
    and — for exactly one call — the product repo's PR body. The OCC mint above
    is scoped ``repositories: onex_change_control``, so reusing it for the
    product PATCH is a guaranteed 403 (live: omnimarket#1958, run
    30496115784). This pins credential SEPARATION rather than broadening the
    least-privilege OCC-Writer App onto product repos.
    """
    mutate_only = "${{ vars.OMNI_OCC_AUTOAUTHOR_MODE == 'mutate' }}"

    validate = _author_step("Validate product-repo write credential")
    assert validate["if"] == mutate_only
    assert validate["env"]["ONEXBOT_APP_ID"] == "${{ secrets.ONEXBOT_APP_ID }}"
    assert (
        validate["env"]["ONEXBOT_APP_PRIVATE_KEY"]
        == "${{ secrets.ONEXBOT_APP_PRIVATE_KEY }}"
    )
    assert (
        'if [ -z "${ONEXBOT_APP_ID:-}" ] || [ -z "${ONEXBOT_APP_PRIVATE_KEY:-}" ]; then'
        in validate["run"]
    )
    assert "::error::" in validate["run"]
    assert "exit 1" in validate["run"]

    mint = _author_step("Mint product-repo write token")
    assert mint["id"] == "product-app-token"
    assert mint["if"] == mutate_only
    assert str(mint["uses"]).startswith("actions/create-github-app-token@")
    # The GENERAL OnexBot App, NOT the OCC-Writer App — a different identity on
    # purpose. Asserting inequality is the load-bearing half: it fails if a
    # later edit "simplifies" both mints onto one App.
    assert mint["with"]["app-id"] == "${{ secrets.ONEXBOT_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.ONEXBOT_APP_PRIVATE_KEY }}"
    assert mint["with"]["app-id"] != "${{ secrets.ONEXBOT_OCC_APP_ID }}"
    # Scoped to THIS repo only — never org-wide, never onex_change_control.
    assert mint["with"]["owner"] == "${{ github.repository_owner }}"
    assert mint["with"]["repositories"] == "${{ github.event.repository.name }}"
    assert mint["with"]["repositories"] != "onex_change_control"

    run = _author_step("Run node_occ_companion_effect")
    assert (
        run["env"]["PRODUCT_WRITE_TOKEN"]
        == "${{ steps.product-app-token.outputs.token }}"
    )
    # Fail-loud (`:?`), never a silent fallback to the OCC token that 403s.
    assert (
        'export OMNI_OCC_PRODUCT_TOKEN="${PRODUCT_WRITE_TOKEN:?mode=mutate requires the minted OnexBot product-repo token}"'
        in run["run"]
    )


@pytest.mark.unit
def test_author_workflow_does_not_stamp_product_body_with_ambient_token() -> None:
    """OMN-15441: ``secrets.GITHUB_TOKEN`` must not become the stamp credential.

    It is product-scoped and *would* be authorized, which makes it the tempting
    "simplification" — but a body edit authored by GITHUB_TOKEN does not emit
    ``pull_request: edited`` (GitHub's recursion guard), and
    ``call-occ-preflight.yml`` lists ``edited`` in its trigger types so the
    stamp re-evaluates eligibility. Using the ambient token would land the
    bytes and silently fail to unjam the PR — a false-GREEN worse than the 403.
    """
    run = _author_step("Run node_occ_companion_effect")
    assert "OMNI_OCC_PRODUCT_TOKEN" in run["run"]
    # The product credential is threaded from the App mint, not the ambient token.
    assert 'OMNI_OCC_PRODUCT_TOKEN="${{ secrets.GITHUB_TOKEN }}"' not in run["run"]
    assert run["env"].get("OMNI_OCC_PRODUCT_TOKEN") != "${{ secrets.GITHUB_TOKEN }}"


@pytest.mark.unit
def test_author_workflow_does_not_reference_the_unprovisioned_pat() -> None:
    """OMN-15350: ``secrets.OCC_AUTOAUTHOR_TOKEN`` was never provisioned.

    The author workflow must not read it as a credential again (a historical
    mention in a comment is fine). This is the companion half of
    ``test_observation_store_does_not_reference_the_unprovisioned_pat``.
    """
    assert "secrets.OCC_AUTOAUTHOR_TOKEN" not in _AUTHOR.read_text(encoding="utf-8")


@pytest.mark.unit
def test_author_workflow_rejects_the_org_wide_pat() -> None:
    """Rolling plan §2 A3: the org-wide CROSS_REPO_PAT is explicitly not used."""
    assert "secrets.CROSS_REPO_PAT" not in _AUTHOR.read_text(encoding="utf-8")


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
    (a per-run OnexBot-OCC-Writer App installation token, OMN-14955), asserted
    below.
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
    """The write credential is minted per-run, scoped to one repo, fail-LOUD.

    OMN-14955: the OMN-14904 ``secrets.OCC_AUTOAUTHOR_TOKEN`` PAT was never
    provisioned; the credential is the least-privilege OnexBot-OCC-Writer
    GitHub App (org secrets ``ONEXBOT_OCC_APP_ID``/``ONEXBOT_OCC_PRIVATE_KEY``,
    the A3/E2 credential), minted via the same canonical
    ``actions/create-github-app-token`` pattern ``pr-arch-review.yml`` uses and
    scoped down to ``OmniNode-ai/onex_change_control`` only.
    """
    validate = _attest_step("Validate OCC cross-repo write credential")
    assert validate["if"] == "${{ steps.obsmode.outputs.mode == 'mutate' }}"
    assert validate["env"]["ONEXBOT_OCC_APP_ID"] == "${{ secrets.ONEXBOT_OCC_APP_ID }}"
    assert (
        validate["env"]["ONEXBOT_OCC_PRIVATE_KEY"]
        == "${{ secrets.ONEXBOT_OCC_PRIVATE_KEY }}"
    )
    # Absent secrets must FAIL the job, not silently degrade to dry_run.
    assert (
        'if [ -z "${ONEXBOT_OCC_APP_ID:-}" ] || [ -z "${ONEXBOT_OCC_PRIVATE_KEY:-}" ]; then'
        in validate["run"]
    )
    assert "::error::" in validate["run"]
    assert "exit 1" in validate["run"]

    mint = _attest_step("Mint OCC write token")
    assert mint["id"] == "occ-app-token"
    assert mint["if"] == "${{ steps.obsmode.outputs.mode == 'mutate' }}"
    assert str(mint["uses"]).startswith("actions/create-github-app-token@")
    assert mint["with"]["app-id"] == "${{ secrets.ONEXBOT_OCC_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.ONEXBOT_OCC_PRIVATE_KEY }}"
    # Scoped-down installation token: one org, ONE repository. Without these
    # `with:` keys the token would cover every repo the App is installed on.
    assert mint["with"]["owner"] == "OmniNode-ai"
    assert mint["with"]["repositories"] == "onex_change_control"

    write = _attest_step("Run node_occ_observation_effect")
    assert write["env"]["OCC_WRITE_TOKEN"] == "${{ steps.occ-app-token.outputs.token }}"
    assert write["env"]["OBS_MODE"] == "${{ steps.obsmode.outputs.mode }}"
    assert 'if [ "$OBS_MODE" = "mutate" ]; then' in write["run"]
    assert (
        'export GITHUB_TOKEN="${OCC_WRITE_TOKEN:?mode=mutate requires the minted OnexBot-OCC-Writer token}"'
        in write["run"]
    )


@pytest.mark.unit
def test_observation_store_does_not_reference_the_unprovisioned_pat() -> None:
    """OMN-14955: ``secrets.OCC_AUTOAUTHOR_TOKEN`` was never provisioned (live
    readback on OMN-14904) — the attestation workflow must not read it as a
    credential again (a historical mention in comments is fine). The legacy
    ``call-occ-companion-author.yml`` Toggle-1 path still names it; that
    workflow is default-off dry_run and its retirement is tracked under A3.
    """
    assert "secrets.OCC_AUTOAUTHOR_TOKEN" not in _ATTEST.read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# OMN-15300: the OCC PR-event workflow family must serialize per PR.
# ---------------------------------------------------------------------------

#: Every omnimarket workflow driven by `pull_request` that acts on the OCC
#: evidence surface. The family shared one gap: none declared `concurrency`, so a
#: PR receiving several events in quick succession ran the whole family several
#: times over. For the observation producer that was not merely wasted CI — each
#: run wrote its own attempt-scoped record and opened its own OCC PR, which is how
#: one head sha produced three PRs in 29 seconds.
_OCC_PR_EVENT_WORKFLOWS = (
    "call-occ-attestation-observe.yml",
    "call-occ-autobind.yml",
    "call-occ-companion-observe.yml",
)


@pytest.mark.unit
@pytest.mark.parametrize("filename", _OCC_PR_EVENT_WORKFLOWS)
def test_occ_pr_event_workflow_declares_concurrency(filename: str) -> None:
    """Each family member serializes on its PR and cancels superseded runs."""
    workflow = _load(_WORKFLOWS / filename)
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, (
        f"{filename} declares no `concurrency` group, so duplicate pull_request "
        "events for one PR each run the whole workflow"
    )
    assert concurrency.get("cancel-in-progress") is True, (
        f"{filename} must cancel superseded runs; queueing them preserves the burn"
    )
    group = concurrency["group"]
    assert "github.event.pull_request.number" in group, (
        f"{filename} concurrency group must be scoped per PR, got: {group}"
    )


@pytest.mark.unit
def test_observer_workflows_key_concurrency_on_head_sha() -> None:
    """Observers key on head sha; the mutating publisher deliberately does not.

    An observer produces one record per sha, so a new push must NOT cancel the
    observation for the previous sha — only redundant runs for the SAME sha are
    collapsed, and those emit byte-identical output. The autobind publisher is the
    opposite case: it rewrites the PR body, so two publishers racing on one PR is
    a lost-update hazard and the newest state must always win.
    """
    for filename in (
        "call-occ-attestation-observe.yml",
        "call-occ-companion-observe.yml",
    ):
        group = _load(_WORKFLOWS / filename)["concurrency"]["group"]
        assert "github.event.pull_request.head.sha" in group, (
            f"{filename} is an observer and must not drop a per-sha observation"
        )

    autobind = _load(_WORKFLOWS / "call-occ-autobind.yml")["concurrency"]["group"]
    assert "head.sha" not in autobind, (
        "call-occ-autobind mutates the PR; keying on head sha would let two "
        "publishers run concurrently for different shas on the same PR"
    )
