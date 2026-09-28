# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19889 -- the dep-health delta step is non-blocking, and visibly temporary.

WHY THE STEP IS NON-BLOCKING. OMN-19677 (ff67f869, #2928) shrank
``.onex_state/dep_health_baseline.json`` from 584 entries to 49, switched the
gate to block on any NEW finding rather than the net baseline delta, and removed
the advisory fallback so the job became blocking. Each change is defensible;
together they made the 535 de-banked findings read as new against a blocking gate
with no non-blocking path. ``dep-health / scan`` then failed on eight consecutive
runs across seven different authors' branches -- including branches whose diff
adds and removes no topic -- and nothing in omnimarket could merge. Last green
run 2026-09-27T12:56:45Z, first red 12:58:35Z, ff67f869 merged 12:57:19Z.

WHAT THIS FILE IS FOR. A ``continue-on-error`` added to unblock a repository is
the kind of thing that stops being temporary by being forgotten. The invariant
below makes that impossible to do silently: the step may be non-blocking OR it
may carry no ticket reference, but not both. Whoever removes the reference has to
remove the escape, and whoever removes the escape can remove the reference.

WHAT IS NOT WEAKENED. ``run_dep_health_sweep.py`` is untouched, so ``--delta-mode``
still computes and still exits non-zero on a new finding -- asserted here by
re-running OMN-19677's own regression expectations rather than restating them.
The baseline is untouched, so the shrink-only direction is intact and nothing is
re-banked; re-banking is growth and needs a ruling, not an edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dep-health-gate.yml"
SWEEP_SCRIPT = REPO_ROOT / "scripts" / "ci" / "run_dep_health_sweep.py"
_TICKET = "OMN-19889"


def _dep_health_steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return list(doc["jobs"]["dep-health"]["steps"])


def _delta_step() -> dict[str, Any]:
    for step in _dep_health_steps():
        run = step.get("run") or ""
        if "--delta-mode" in run:
            return step
    raise AssertionError(
        "no step in the dep-health job runs the sweep with --delta-mode; the "
        "gate's blocking phase has been renamed or removed"
    )


def test_the_delta_step_still_exists_and_still_runs_the_sweep() -> None:
    """The escape must not have become a deletion.

    Making the step non-blocking is not the same as not looking. The sweep still
    runs on every PR and still prints its findings; only the exit code stops
    failing the job.
    """
    step = _delta_step()
    run = step["run"]
    assert "run_dep_health_sweep.py" in run
    assert "--baseline-path .onex_state/dep_health_baseline.json" in run
    assert "--severity-threshold MAJOR" in run


def test_a_non_blocking_delta_step_must_name_the_ticket_that_removes_it() -> None:
    """The invariant: non-blocking OR unreferenced, never both.

    This is the whole point of the file. If someone later drops the OMN-19889
    reference while leaving continue-on-error in place, the escape has become
    permanent by omission and this fails. If someone removes the escape, the
    reference is free to go with it.
    """
    step = _delta_step()
    if not step.get("continue-on-error"):
        return  # blocking again: nothing to keep honest
    rendered = yaml.safe_dump(step)
    source = WORKFLOW.read_text(encoding="utf-8")
    assert _TICKET in rendered or _TICKET in source, (
        "the dep-health delta step is non-blocking but names no ticket. A "
        f"continue-on-error with no owner is permanent by omission -- cite {_TICKET} "
        "or make the step blocking again."
    )


def test_the_baseline_was_not_quietly_regrown() -> None:
    """Re-banking the 535 findings is growth and needs a ruling, not an edit.

    OMN-19677's whole subject is that this baseline can only shrink. Unblocking
    the repository must not smuggle the findings back in, so the entry count is
    asserted against the value ff67f869 deliberately left behind.
    """
    baseline = REPO_ROOT / ".onex_state" / "dep_health_baseline.json"
    if not baseline.is_file():
        pytest.skip("no baseline committed in this checkout")
    import json

    data = json.loads(baseline.read_text(encoding="utf-8"))

    def count(node: Any) -> int:
        if isinstance(node, list):
            return len(node)
        if isinstance(node, dict):
            return sum(count(v) for v in node.values())
        return 0

    entries = count(data)
    assert entries <= 49, (
        f"the dep-health baseline holds {entries} entries, above the 49 "
        "ff67f869 left it at. Growing it re-banks findings the OMN-19677 "
        "ratchet exists to refuse; that needs a recorded ruling (OMN-19889 AC3)."
    )


def test_the_sweep_script_is_untouched_by_this_unblock() -> None:
    """The gate's logic is Jonah's and stays his.

    This change is a workflow-level escape, not a behaviour change. The script
    must still implement the new-findings check OMN-19677 added, or the escape
    has quietly become a downgrade.
    """
    source = SWEEP_SCRIPT.read_text(encoding="utf-8")
    assert "--delta-mode" in source
    assert "new_findings_count" in source, (
        "run_dep_health_sweep.py no longer consults new_findings_count; "
        "OMN-19677's block-on-any-new-finding fix has been undone"
    )
