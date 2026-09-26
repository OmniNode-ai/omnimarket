# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19802: the rebuild trigger's pin carries the node_inventory ordering fix.

The reusable runs omnibase_infra's ``lab_pass_receipt.py`` at the pinned commit
(it checks its scripts out at ``github.job_workflow_sha``). Below omnibase_infra
04eb6d41d (#4175), ``probe-lane`` read the lane's introspection manifest BEFORE
its settle wait. On a lane the deploy agent had just recreated, that GET was
refused, so every omnimarket compose-dev receipt from 2026-09-26 11:21Z on
FAILED on ``node_inventory`` alone. Measured on artifact 10909257483 (sha
befa9cc0ab): ``Errno 111`` on port 8085 beside a 200 from ``ready_main`` on
the same port. Every omnimarket runtime PR then waited on a lab receipt that
could not pass.

A pin revert below 04eb6d41d reopens that with every other test in this
directory green, so it is refused here by name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"
REUSABLE = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)

#: omnibase_infra#4175, the commit that reads the manifest after the wait.
_NODE_INVENTORY_FIX_COMMIT: Final[str] = "04eb6d41d"

#: Pins recorded as carrying ``_NODE_INVENTORY_FIX_COMMIT``, each with its
#: evidence. Add a pin here only after
#: `git merge-base --is-ancestor 04eb6d41d <pin>` exits 0 in an omnibase_infra
#: clone.
_PINS_CARRYING_THE_NODE_INVENTORY_FIX: Final[dict[str, str]] = {
    "04eb6d41d1ea8364ef41a3c5ed62bd5743014bdc": (
        "omnibase_infra#4175 (OMN-19802) itself, merged 2026-09-26"
    ),
}

_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


def _rebuild_job() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    matching = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and REUSABLE in str(job.get("uses") or "")
    ]
    assert len(matching) == 1, f"expected exactly one caller of {REUSABLE}"
    return matching[0]


def test_the_pin_carries_the_node_inventory_ordering_fix() -> None:
    """RED at any pin below 04eb6d41d, including the 89d4dd6c it replaces."""
    uses = str(_rebuild_job()["uses"])
    _, _, ref = uses.partition("@")
    assert _FULL_SHA.match(ref), f"`uses: ...@{ref}` is not a full commit SHA"
    assert ref in _PINS_CARRYING_THE_NODE_INVENTORY_FIX, (
        f"the reusable is pinned to {ref}, which is not recorded as carrying "
        f"omnibase_infra {_NODE_INVENTORY_FIX_COMMIT} (OMN-19802). Below it, "
        "probe-lane reads the introspection manifest before the settle wait, "
        "and a compose-dev receipt FAILs node_inventory on a freshly recreated "
        "lane. If the new pin does carry it, prove it with `git merge-base "
        f"--is-ancestor {_NODE_INVENTORY_FIX_COMMIT} {ref}` and add it to "
        "_PINS_CARRYING_THE_NODE_INVENTORY_FIX with that evidence."
    )
