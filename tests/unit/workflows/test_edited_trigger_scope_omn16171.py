# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression guard: only body/title/base-reading gates may fire on `edited`.

OMN-16171. GitHub's `pull_request` default activity types are
``[opened, synchronize, reopened]``. Adding ``edited`` makes a workflow re-run
whenever a PR's title, body, or base branch changes -- with no new head SHA and
therefore no new source to inspect. That is correct and load-bearing for gates
that PARSE the PR body/title/base (the Receipt Gate's ``OMN-XXXX`` citation, the
skip-token scan that root CLAUDE.md rule 10 depends on, the base-retarget
guards) and for every ``occ-preflight`` eval-path caller, whose stale FAILURE
must be cleared by a post-open ``Evidence-Source:`` stamp (OMN-14241,
guarded separately by ``test_occ_eval_path_trigger_coverage.py``).

It is pure waste for a gate that only reads source at the head SHA: the verdict
on an unchanged SHA is identical every time, so each edit re-queues the whole
workflow for a result already computed. And the edits are not rare -- OCC's own
evidence-stamp bots rewrite product-PR bodies using a minted App token, which
DOES emit ``pull_request: edited`` (GitHub's recursion guard suppresses only
edits authored by the ambient ``GITHUB_TOKEN``), so the evidence pipeline feeds
itself.

Measured live 2026-08-18 while the org-wide Actions backlog stood at 247 queued
jobs: omnibase_infra#2784 drew three full trigger waves against ONE unchanged
head SHA (7993b115) in 23 minutes -- 13:28:50Z (42 workflows, the real
`synchronize` wave), 13:50:09Z (10 workflows) and 13:51:33Z (8 workflows), the
latter two body-edit-driven. Separately, one shared OCC observation branch took
114 CI runs across 10 force-push waves in a 45-minute window.

This module pins the split in both directions, because only the second half is
safety-relevant: narrowing a gate that genuinely needs ``edited`` would make it
silently stop re-evaluating, and a missing trigger produces SILENCE (no run),
never a red check that would prompt investigation.

Why types-narrowing rather than a job-level ``if:`` short-circuit: a job skipped
by an ``if:`` still publishes a check run with conclusion ``skipped``, which
GitHub branch protection counts as passing -- and which omnibase_infra's
``ci_summary_gate.py`` external-context assertion treats as a hard failure
(``EXTERNAL_GOOD_CONCLUSIONS = {"success"}``). Either way the short-circuit
pattern is wrong here. Removing the trigger type creates no run and no check
run at all, so the passing check run from the SHA's ``opened``/``synchronize``
wave stands untouched -- and every head SHA necessarily receives one of those,
since a SHA cannot first appear via ``edited``.

Accepted residual, stated rather than hidden: a base-branch retarget also
arrives as ``edited``, so the narrowed gates will not re-evaluate against a new
base until the next push. That matches the repo's pre-existing baseline (most
workflows here, ``ci.yml`` included, already ran on the default types and never
re-ran on retarget), and ``main-target-guard`` / ``non-dev-base-guard`` keep
``edited`` precisely to catch a bad retarget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Gates that read ONLY source at the head SHA. Narrowed by OMN-16171: they must
# never regain `edited`.
MUST_NOT_LIST_EDITED: tuple[str, ...] = (
    "canonical-inference-gate.yml",
    "url-authority-gate.yml",
    "no-faked-boundary.yml",
    "occ-emitter-golden-gate.yml",
    "projection-exposure-drift-gate.yml",
    "reject-leaked-literals.yml",
    "call-occ-attestation-observe.yml",
)

# Gates whose verdict depends on the PR body, title, or base branch, or which
# call `occ-preflight` and must self-heal a post-open evidence stamp. Losing
# `edited` here is the dangerous direction -- it is silent.
MUST_LIST_EDITED: tuple[str, ...] = (
    "pr-title-check.yml",
    "call-receipt-gate.yml",
    "call-reject-skip.yml",
    "main-target-guard.yml",
    "non-dev-base-guard.yml",
    "call-occ-preflight.yml",
)


def _pull_request_types(workflow_path: Path) -> set[str]:
    """Return ``on.pull_request.types``, defaulting as GitHub does when absent."""
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # PyYAML 1.1 resolves a bare `on:` key to the boolean True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), f"{workflow_path}: no mapping `on:` block"
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), (
        f"{workflow_path}: expected a mapping `pull_request:` trigger"
    )
    types = pull_request.get("types")
    if types is None:
        return {"opened", "synchronize", "reopened"}
    assert isinstance(types, list), f"{workflow_path}: `types:` is not a list"
    assert types, f"{workflow_path}: `types:` is present but empty"
    return set(types)


@pytest.mark.unit
@pytest.mark.parametrize("workflow_name", MUST_NOT_LIST_EDITED)
def test_source_only_gate_does_not_fire_on_edited(workflow_name: str) -> None:
    path = WORKFLOWS_DIR / workflow_name
    assert path.exists(), f"{path} is missing -- update MUST_NOT_LIST_EDITED"
    assert "edited" not in _pull_request_types(path), (
        f"{workflow_name} lists `edited` again. This gate reads only source at "
        f"the head SHA, so a body/title edit cannot change its verdict -- every "
        f"such run re-queues work already done. If this gate has genuinely "
        f"gained a PR-body, title, or base-branch dependency, move it to "
        f"MUST_LIST_EDITED and say why in the same commit."
    )


@pytest.mark.unit
@pytest.mark.parametrize("workflow_name", MUST_LIST_EDITED)
def test_body_reading_gate_still_fires_on_edited(workflow_name: str) -> None:
    path = WORKFLOWS_DIR / workflow_name
    assert path.exists(), f"{path} is missing -- update MUST_LIST_EDITED"
    assert "edited" in _pull_request_types(path), (
        f"{workflow_name} no longer lists `edited`. Its verdict depends on the "
        f"PR body, title, or base branch, so without this trigger a post-open "
        f"edit leaves the previous run's result standing forever -- and the "
        f"failure mode is silence, not a red check."
    )


@pytest.mark.unit
def test_narrowed_gates_declare_no_occ_preflight_job() -> None:
    """A narrowed gate must not be an occ-preflight eval-path caller.

    Those callers need `edited` to clear a stale `occ-preflight / eligibility`
    FAILURE after an `Evidence-Source:` stamp (OMN-14241). If one ever acquires
    an `occ-preflight` job it must leave MUST_NOT_LIST_EDITED in the same
    commit, so this fails closed rather than degrading quietly.
    """
    offenders = []
    for workflow_name in MUST_NOT_LIST_EDITED:
        data = yaml.safe_load(
            (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
        )
        for job_id, job in (data.get("jobs") or {}).items():
            uses = (job or {}).get("uses")
            if job_id == "occ-preflight" or (
                isinstance(uses, str) and "occ-preflight.yml" in uses
            ):
                offenders.append(f"{workflow_name}:{job_id}")
    assert not offenders, (
        f"these narrowed gates now call occ-preflight and so need `edited` "
        f"back: {offenders}"
    )


@pytest.mark.unit
def test_extraction_reports_the_github_default_for_a_types_less_workflow(
    tmp_path: Path,
) -> None:
    """Proves the parser is not green-by-absence.

    A workflow with no `types:` key runs on GitHub's implicit default, which
    excludes `edited`. If this returned an empty set instead, every
    MUST_LIST_EDITED assertion would silently pass for the wrong reason.
    """
    fixture = tmp_path / "fixture.yml"
    fixture.write_text("on:\n  pull_request:\n    branches: [dev]\n")
    assert _pull_request_types(fixture) == {"opened", "synchronize", "reopened"}
