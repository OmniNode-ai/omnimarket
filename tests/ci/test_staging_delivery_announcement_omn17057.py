# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17057: this repository hands over the credentials that carry its merges.

WHAT WAS MISSING, measured live 2026-09-16.

A merge here could not cause a delivery to ``onex-dev``. It rode along on
whatever the next ``omnibase_infra`` push happened to build, and that candidate
clones every sibling at branch ``dev`` at that instant -- so the omnimarket
revision baked into the staging image was never CHOSEN and no merge here could
prove delivery of its own sha::

    omnibase_infra delivery run 35013603978 (2026-09-15T19:26Z, success)
      build manifest per_sibling_vcs_provenance.omnimarket.vcs_ref
        = 3038d1dabadea4973707f0228862b4aef4e09242

``3038d1da`` merged here at 17:08Z and reached a candidate at 19:26Z because an
unrelated commit landed over there. Twelve commits had landed on top of it by
the time this was read.

``omnibase_infra#3611`` added the job that closes it: after
``verify-sibling-converged`` proves the ``.201`` dev lane vendors this
repository's merged sha and publishes the compose-dev lab-pass receipt for it,
a terminal job announces that revision to the staging delivery workflow. That
job crosses a repository boundary, so it cannot use this repository's own
token -- it needs the org App pair, which a called workflow can only receive
from its caller.

This repository's half is therefore two lines: advance the pin onto a commit
that carries the job, and hand it the credentials. Both are pinned below, and
the pin itself is already guarded by ``test_rebuild_trigger_pin_omn18387.py``.

WHY THE SECRETS ARE ASSERTED AND NOT LEFT TO REVIEW.

Dropping them is silent. The reusable declares them ``required: false`` so that
an existing caller keeps compiling, and the announcement job then refuses at run
time rather than skipping -- but a caller that stops passing them turns every
merge here into a red announcement nobody is watching for, which is one step
away from the silent non-delivery this whole ticket is about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"

REUSABLE = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)

#: What the announcement job needs, and what the publisher needs. Named
#: together because the caller supplies both to one reusable workflow.
REQUIRED_SECRETS = (
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "ONEXBOT_OCC_APP_ID",
    "ONEXBOT_OCC_PRIVATE_KEY",
)


def _caller_job() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matching = [
        job
        for job in (doc.get("jobs") or {}).values()
        if isinstance(job, dict) and REUSABLE in str(job.get("uses") or "")
    ]
    assert len(matching) == 1, (
        f"expected exactly one job calling {REUSABLE}, got {len(matching)}"
    )
    return matching[0]


@pytest.mark.unit
def test_the_caller_hands_over_every_secret_the_reusable_declares() -> None:
    declared = _caller_job().get("secrets") or {}
    assert not isinstance(declared, str), (
        "the caller passes `secrets: inherit`. That hands the reusable every "
        "secret this repository holds, which is a strictly wider grant than the "
        "four it declares -- name them"
    )
    missing = [name for name in REQUIRED_SECRETS if name not in declared]
    assert not missing, (
        f"runtime-rebuild-trigger.yml does not pass {missing} to the reusable "
        "workflow. Without the App pair the announcement job refuses, so every "
        "merge here converges on the lab and then goes nowhere"
    )


@pytest.mark.unit
def test_every_passed_secret_is_read_from_the_secrets_context() -> None:
    """A literal here would be a credential in the tree, not a reference to one."""
    declared = _caller_job()["secrets"]
    for name in REQUIRED_SECRETS:
        expression = str(declared[name]).strip()
        assert expression == "${{ secrets." + name + " }}", (
            f"{name} is passed as {expression!r} rather than a reference to the "
            "secrets context"
        )


@pytest.mark.unit
def test_the_caller_still_supplies_the_merge_sha_the_delivery_pins() -> None:
    """The announced revision is this input. A branch here re-floats the pin."""
    with_block = _caller_job()["with"]
    source_sha = str(with_block["source_sha"])
    assert "merge_commit_sha" in source_sha, (
        f"source_sha is {source_sha!r}. The announcement pins the staging "
        "candidate to this value, so anything but the merge commit reinstates "
        "the floating ref OMN-17057 exists to remove"
    )
