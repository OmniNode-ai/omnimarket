# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20008: the rebuild trigger's pin carries the sibling census fetch.

The reusable runs omnibase_infra's ``lab_pass_receipt.py`` at the pinned commit
(it checks its scripts out at ``github.job_workflow_sha``). Below omnibase_infra
cb5c0cfd7 (#4900, OMN-20934), the sibling runner sees the routed lane's docker
socket but has no private census configured for ``lane_sync``.

Measured without the fetch: trigger run 38293893148 for merge
886690e96602425262fbe59310a5d7129200189b (omnimarket#3819) wrote artifact
11768686435, the routed sibling lane's lab-pass receipt, with result FAIL
at 2026-10-12T19:07:49Z. Every check was ok except ``lane_sync``:
``census unreadable: ValueError: census exit=3: ... ERROR: the lane census is
not configured: neither ONEX_WORKSPACE_CONFIG_ROOT nor OMNI_HOME is set``.
That FAIL holds the omnimarket 0.4.312 release (release-cut tick reason
``lab_receipt_absent``).

#4900 fetches the private census with ``./.github/actions/fetch-census-overlay``
(#4901, 3e9cd7fe4, an ancestor), ``vars.OMNI_CI_OVERLAY_REPO`` and the App
credentials this caller already passes. A revert to 04eb6d41d reopens the
failure while the older pin tests stay green, so it is refused here by name.
This workflow-only bump triggers no rebuild; runtime proof is the next
omnimarket runtime merge's sibling receipt carrying ``lane_sync`` ok.
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

#: omnibase_infra#4900 (OMN-20934), the sibling private census fetch.
_SIBLING_CENSUS_FIX_COMMIT: Final[str] = "cb5c0cfd7"

#: Pins recorded as carrying ``_SIBLING_CENSUS_FIX_COMMIT``, each with evidence.
#: Add a pin only after `git merge-base --is-ancestor cb5c0cfd7 <pin>` exits 0
#: in an omnibase_infra clone.
_PINS_CARRYING_THE_SIBLING_CENSUS_FIX: Final[dict[str, str]] = {
    "cb5c0cfd724905aec46d336dea84e8347cce9e20": (
        "omnibase_infra#4900 (OMN-20934) itself, merged 2026-10-12T17:16:00Z; "
        "fetch-census-overlay ancestor 3e9cd7fe4 (#4901), `git merge-base "
        "--is-ancestor 3e9cd7fe4 cb5c0cfd724905aec46d336dea84e8347cce9e20` "
        "-> exit 0, verified 2026-10-12"
    ),
    "dev-20261028175438-79bc8a26389c": (
        "omnibase_infra dev-merge tag of 79bc8a26389c; `git merge-base --is-ancestor "
        "cb5c0cfd7 dev-20261028175438-79bc8a26389c` -> exit 0, verified 2026-10-28"
    ),
}

_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_DEV_MERGE_TAG: Final[re.Pattern[str]] = re.compile(r"^dev-\d{14}-[0-9a-f]{12}$")


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


def test_the_pin_carries_the_sibling_census_fetch() -> None:
    """RED at any pin below cb5c0cfd7, including the 04eb6d41d it replaces."""
    uses = str(_rebuild_job()["uses"])
    _, _, ref = uses.partition("@")
    assert _FULL_SHA.match(ref) or _DEV_MERGE_TAG.match(ref), (
        f"`uses: ...@{ref}` is not a full commit SHA or a dev-merge tag"
    )
    assert ref in _PINS_CARRYING_THE_SIBLING_CENSUS_FIX, (
        f"the reusable is pinned to {ref}, which is not recorded as carrying "
        f"omnibase_infra {_SIBLING_CENSUS_FIX_COMMIT} (#4900, OMN-20934). "
        "Below it, sibling lab-pass receipts FAIL lane_sync with census "
        "unreadable (artifact 11768686435), holding the omnimarket release. "
        "If the new pin does carry it, prove it with `git merge-base "
        f"--is-ancestor {_SIBLING_CENSUS_FIX_COMMIT} {ref}` and add it to "
        "_PINS_CARRYING_THE_SIBLING_CENSUS_FIX with that evidence."
    )
