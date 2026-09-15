# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18387: this repo's pin of omnibase_infra's sibling rebuild trigger.

WHAT BROKE, measured live 2026-09-15.

``runtime-rebuild-trigger.yml`` calls omnibase_infra's
``runtime-rebuild-trigger-reusable.yml`` at a commit SHA. That pin governs which
workflow FILE loads and nothing else -- it could not reach the
``actions/checkout`` INSIDE that file, which was written ``ref: dev``. So the
YAML spelling ``scripts/ci/lab_pass_receipt.py probe-lane``'s arguments came from
the pinned commit while the script reading them came from whatever omnibase_infra
``dev`` held at run time.

OMN-18387 then made ``--projection-url`` a required argument of that script and
updated its caller in the same commit -- correctly and atomically, over there.
The pinned copy of the YAML here kept invoking the new script without it::

    run 35019423922 (2026-09-15T20:24Z)
      lab_pass_receipt.py probe-lane -> exit 2 (required --projection-url absent)
      -> "refusing to emit an invalid receipt"
      -> FAIL compose-dev lab-pass receipt

Under CLAUDE.md rule 24(b) delivery of a sha to staging fails closed without a
PASS receipt for it, so this is a delivery outage, not a cosmetic one.

The durable fix is in omnibase_infra (``omnibase_infra#3601``): the guard job now
checks its scripts out at ``inputs.infra_ref || github.job_workflow_sha``, so the
YAML and the script it invokes are one commit behind the single pin below and
cannot diverge again. This repository's half is to advance the pin onto a commit
that carries it.

These tests guard the two ways the fix can be undone from this side:

1. replacing the SHA with a branch name, which reopens the drift at the outer
   pin instead of the inner one;
2. passing ``infra_ref:`` explicitly, which is the override the new input exists
   to allow and is also exactly how a caller would re-float the inner checkout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"

REUSABLE = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)

# The pinned YAML must invoke probe-lane with this argument. Before
# omnibase_infra#3601 the pin was e95eb9ba1bbf3a76924d32cf0925d830a135edcb, whose
# copy of the workflow does not, which is the whole of the defect above.
_SUPERSEDED_PIN = "e95eb9ba1bbf3a76924d32cf0925d830a135edcb"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _rebuild_job() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    matching = {
        job_id: job
        for job_id, job in jobs.items()
        if isinstance(job, dict) and REUSABLE in str(job.get("uses") or "")
    }
    assert matching, (
        f"{WORKFLOW.name} no longer calls {REUSABLE}. If the rebuild trigger moved, "
        "move these assertions with it rather than deleting them -- the skew they "
        "guard is a property of the call, not of the file name."
    )
    assert len(matching) == 1, (
        f"expected exactly one caller job, got {sorted(matching)}"
    )
    return next(iter(matching.values()))


def test_the_reusable_rebuild_trigger_is_pinned_to_a_full_commit_sha() -> None:
    """A branch name here reopens the drift at the outer pin."""
    uses = str(_rebuild_job()["uses"])
    _, _, ref = uses.partition("@")

    assert ref, f"`uses: {uses}` carries no @ref at all"
    assert _FULL_SHA.match(ref), (
        f"`uses: ...@{ref}` is not a 40-character commit SHA. A branch name makes "
        "the workflow file itself float, so the YAML this repo runs changes under "
        "it with no commit here -- the same class of drift, moved one level out."
    )
    assert ref != _SUPERSEDED_PIN, (
        f"the pin is back on {_SUPERSEDED_PIN}, whose copy of the reusable workflow "
        "invokes lab_pass_receipt.py probe-lane without the --projection-url that "
        "the script requires. That combination emits a FAIL compose-dev lab-pass "
        "receipt on every omnimarket merge (run 35019423922), and under rule 24(b) "
        "a FAIL receipt blocks delivery of that sha to staging."
    )


def test_the_caller_does_not_re_float_the_guards_script_checkout() -> None:
    """``infra_ref:`` is an override, and overriding it is how the defect returns.

    omnibase_infra#3601 defaults the guard's own checkout to
    ``github.job_workflow_sha``. Passing ``infra_ref: dev`` from here would put
    the scripts back on a moving branch under a pinned YAML, which is precisely
    the configuration that broke. Leave it unset.
    """
    with_block = _rebuild_job().get("with") or {}
    assert "infra_ref" not in with_block, (
        "runtime-rebuild-trigger.yml passes `infra_ref` to the reusable workflow. "
        "Its default is github.job_workflow_sha -- the commit the `uses: ...@<sha>` "
        "pin above already names -- which is what keeps the YAML and the scripts it "
        "invokes on one commit. Overriding it re-floats the inner checkout and "
        "reopens OMN-18387. Remove the override."
    )
