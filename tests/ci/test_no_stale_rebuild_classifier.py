# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: the dormant local rebuild classifier stays deleted [OMN-19378].

``scripts/trigger_rebuild_on_merge.py`` was never invoked by this repository's
live CI: OMN-18268 rewrote ``runtime-rebuild-trigger.yml`` to delegate the
runtime-affecting decision and the publish entirely to the omnibase_infra
reusable workflow (pinned in that file), which runs the omniclaude main
deploy-gate classifier. The dormant copy's own rule (any changed path under
``src/omnimarket/**``) was coarser than the canonical classifier, and an
external tool read its mere presence on disk as proof this repository
self-classifies -- omnimarket#2817 sat on the runtime train for about 10h on
that misclassification (ledger rows
``docs/tracking/ROLLING_WORK_LEDGER.md:3380,3400,3401``, 2026-09-24).

This suite pins the fix: the script and its dedicated tests are gone, and no
workflow, contract or script in this repository still points at the deleted
path as something to invoke. It deliberately does NOT flag the legitimate
references that remain -- ``ci-bus-overlay-parity.yml`` and
``check_ci_bus_overlay_parity.py`` fetch and read the omnibase_infra copy of
this filename (a different repository's canonical file), and
``omn_15597_occ_census_pinned.py`` carries verbatim, pinned-SHA OCC census
fixtures that are historical artifacts under test, not live callers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DELETED_SCRIPT = REPO_ROOT / "scripts" / "trigger_rebuild_on_merge.py"
DELETED_TESTS = (
    REPO_ROOT / "tests" / "unit" / "scripts" / "test_trigger_rebuild_on_merge.py",
    REPO_ROOT
    / "tests"
    / "unit"
    / "scripts"
    / "test_trigger_producer_effect_assertion.py",
)

# Files that legitimately still name "trigger_rebuild_on_merge.py" because they
# reference the OMNIBASE_INFRA copy (fetched fresh, or pinned as a historical,
# exempted test fixture), never this repository's own deleted file.
_ALLOWED_REFERENCES = {
    REPO_ROOT / ".github" / "workflows" / "ci-bus-overlay-parity.yml",
    REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml",
    REPO_ROOT / "scripts" / "ci" / "check_ci_bus_overlay_parity.py",
    REPO_ROOT / "tests" / "ci" / "test_ci_bus_overlay_parity.py",
    REPO_ROOT
    / "tests"
    / "unit"
    / "nodes"
    / "node_dod_verify"
    / "omn_15597_occ_census_pinned.py",
    # This file and the OMN-9727 historical DoD record name the deleted path
    # deliberately, as commentary/history, not as something to invoke.
    Path(__file__).resolve(),
    REPO_ROOT / "docs" / "work-tracking" / "contracts" / "OMN-9727.yaml",
    REPO_ROOT / "scripts" / "ci_bus_lanes.py",
}

_INVOCATION_RE = re.compile(
    r"(?:python[3]?\s+(?:-m\s+)?|uv\s+run\s+python[3]?\s+)?"
    r"(?:\./)?scripts/trigger_rebuild_on_merge\.py"
)


@pytest.mark.unit
def test_the_dormant_classifier_script_is_deleted() -> None:
    assert not DELETED_SCRIPT.is_file(), (
        f"{DELETED_SCRIPT} must stay deleted (OMN-19378): it was never invoked "
        "by this repository's live CI, and its presence is what an external "
        "tool misread as proof this repository self-classifies."
    )


@pytest.mark.unit
def test_the_dormant_classifiers_dedicated_tests_are_deleted() -> None:
    for path in DELETED_TESTS:
        assert not path.is_file(), (
            f"{path} tested the deleted classifier's own internal logic and "
            "must stay deleted alongside it (OMN-19378)."
        )


@pytest.mark.unit
def test_no_workflow_or_contract_still_invokes_the_deleted_script() -> None:
    """No committed file names the deleted path as a command to run.

    A bare textual mention is fine (history, or a reference to the different,
    still-live omnibase_infra copy); an actual invocation shape
    (``python scripts/trigger_rebuild_on_merge.py`` / ``./scripts/...``) is
    the regression this test exists to catch.
    """
    offenders: list[str] = []
    for pattern in ("*.yml", "*.yaml", "*.py", "*.sh"):
        for candidate in REPO_ROOT.rglob(pattern):
            if candidate.resolve() in _ALLOWED_REFERENCES:
                continue
            if ".git" in candidate.parts or ".venv" in candidate.parts:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _INVOCATION_RE.search(text):
                offenders.append(str(candidate.relative_to(REPO_ROOT)))
    assert not offenders, (
        "the following files still invoke the deleted "
        f"scripts/trigger_rebuild_on_merge.py: {offenders}"
    )


@pytest.mark.unit
def test_runtime_rebuild_trigger_workflow_delegates_and_does_not_run_a_local_script() -> (
    None
):
    """Pins the OMN-18268 shape this ticket's regression depends on."""
    workflow = REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"
    text = workflow.read_text(encoding="utf-8")
    assert (
        "uses: OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml@"
        in text
    ), (
        "the live merge-path workflow must delegate to the pinned omnibase_infra "
        "reusable workflow; if this changes, re-derive whether a local "
        "classifier script is warranted again"
    )
    assert "run: python scripts/trigger_rebuild_on_merge.py" not in text
    assert "run: uv run python scripts/trigger_rebuild_on_merge.py" not in text
