# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliverable 4(b): the OMN-14393 report-only workflows are structurally NON-BLOCKING.

Asserts, against the committed YAML, that both new workflows:
  * mark their job ``continue-on-error: true`` (a red step is observed, never enforced);
  * are separate workflow files whose job ids are NOT referenced by ci.yml's
    required ``CI Summary`` ``needs:`` list (GHA ``needs:`` cannot cross workflow-file
    boundaries — so a failing attestation cannot feed the merge-gating rollup);
and that the mutate-capable author workflow defaults to dry_run (flag OFF) and
never sources its mode from PR input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_AUTHOR = _WORKFLOWS / "call-occ-companion-author.yml"
_ATTEST = _WORKFLOWS / "call-occ-attestation-observe.yml"
_CI = _WORKFLOWS / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "job_id"),
    [
        (_AUTHOR, "occ-companion-author"),
        (_ATTEST, "occ-attestation-observe"),
    ],
)
def test_report_only_job_is_continue_on_error(path: Path, job_id: str) -> None:
    data = _load(path)
    job = data["jobs"][job_id]
    assert job["continue-on-error"] is True, (
        f"{job_id} must be continue-on-error to stay non-blocking"
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
    # Injection-safety: mode is NEVER taken from PR title/body/head_ref.
    assert (
        "github.event.pull_request.title"
        not in text.split("if:", 1)[-1].split("Build author payload")[-1]
    )
    # The payload's mode comes from the validated shell var, not a PR field.
    assert "--arg mode " in text


@pytest.mark.unit
def test_attestation_workflow_has_no_write_permissions() -> None:
    data = _load(_ATTEST)
    perms = data["permissions"]
    # Report-only observer: read-only scopes only.
    assert perms.get("contents") == "read"
    assert perms.get("pull-requests") == "read"
    assert "write" not in set(perms.values())
