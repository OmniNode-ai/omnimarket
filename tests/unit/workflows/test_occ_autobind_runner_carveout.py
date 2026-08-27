# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16691: the inline occ-autobind publisher is pinned to the .201 fleet.

omnimarket runs ``publish-occ-autobind`` INLINE in
``.github/workflows/call-occ-autobind.yml`` rather than through omniclaude's
``call-occ-autobind-reusable.yml``, so it is a second home for the same job and
carries its own copy of the runner carve-out.

Context (OMN-16682 constraint 8). The publisher targets the TAILNET-ONLY
dev-lane broker ``omninode-pc.tail75df5e.ts.net:19092`` declared in this repo's
``config/ci_bus_lanes.yaml``. Only the self-hosted ``omnibase-ci`` fleet on .201
is on that tailnet. Until 2026-08-26 the job selected its runner from the SHARED
``OMNI_TRUSTED_CI_RUNS_ON_JSON`` variable — the same seam that governs every
lint/test/build job in the org, and the seam the OMN-16682 hosted-runner
migration exists to flip.

When that seam was flipped to ``["ubuntu-latest"]`` at 2026-08-26T22:46:41Z the
publisher landed on a GitHub-hosted runner, could not resolve the broker, and
failed loud exactly as OMN-14451/OMN-14639 designed. Because this publisher is
what mints the OCC companion that stamps ``Evidence-Source: OCC#<n>``, and the
receipt gate (OMN-10419) hard-fails without it, the result was not a degraded CI
job — nothing could merge for 45 minutes (incident OMN-16691).

The property pinned here: the trusted branch reads the DEDICATED
``OMNI_OCC_AUTOBIND_RUNS_ON_JSON`` variable with a literal
``["self-hosted","omnibase-ci"]`` fallback, and never the shared seam. The
dedicated variable is deliberately left UNSET, so the literal is the operating
value. A future edit that harmonises this back onto the shared seam reads as a
consistency tidy-up in review; it fails here instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "call-occ-autobind.yml"
)

CARVEOUT_VAR = "OMNI_OCC_AUTOBIND_RUNS_ON_JSON"
SHARED_SEAM_VAR = "OMNI_TRUSTED_CI_RUNS_ON_JSON"
FLEET_LITERAL = '\'["self-hosted","omnibase-ci"]\''


def _runs_on() -> str:
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    workflow = cast("dict[str, Any]", loaded)
    runs_on = workflow["jobs"]["publish-occ-autobind"]["runs-on"]
    assert isinstance(runs_on, str), (
        "publish-occ-autobind must set `runs-on` to a selector expression"
    )
    return runs_on


@pytest.mark.unit
def test_publisher_uses_the_dedicated_carveout_variable() -> None:
    assert CARVEOUT_VAR in _runs_on(), (
        "publish-occ-autobind reaches the tailnet-only dev-lane broker and must "
        f"select its runner from the dedicated `{CARVEOUT_VAR}` variable "
        "(OMN-16691 / OMN-16682 constraint 8)"
    )


@pytest.mark.unit
def test_publisher_is_not_governed_by_the_shared_seam() -> None:
    assert SHARED_SEAM_VAR not in _runs_on(), (
        f"publish-occ-autobind must NOT read `{SHARED_SEAM_VAR}`. That variable "
        "governs ~475 unrelated lint/test/build jobs and exists to be flipped to "
        "ubuntu-latest by the OMN-16682 migration. A hosted runner cannot "
        "resolve omninode-pc.tail75df5e.ts.net:19092, so binding this publisher "
        "to that seam makes a CI migration a merge-wide outage (incident "
        "OMN-16691, 2026-08-26 22:46:41Z-23:32:03Z)."
    )


@pytest.mark.unit
def test_fleet_literal_is_the_operating_value() -> None:
    """The dedicated variable is deliberately UNSET, so the literal is live."""
    assert FLEET_LITERAL in _runs_on(), (
        f"publish-occ-autobind must carry the literal {FLEET_LITERAL} fallback — "
        "with the dedicated variable unset it is the value that actually selects "
        "the runner"
    )


@pytest.mark.unit
def test_fork_isolation_branch_is_unchanged() -> None:
    """OMN-16683: the carve-out re-homes only the TRUSTED branch."""
    runs_on = _runs_on()
    assert "OMNI_PUBLIC_PR_RUNS_ON_JSON" in runs_on, (
        "the fork-PR branch of the selector must survive — fork PRs run on the "
        "public runner class where the publisher skips gracefully (no broker)"
    )
    assert "head.repo.full_name != github.repository" in runs_on, (
        "the fork/non-fork predicate that chooses between the public and "
        "carved-out runner classes must survive"
    )


@pytest.mark.unit
def test_carveout_rationale_is_documented_inline() -> None:
    """A bare variable rename is not self-explaining; the WHY must be adjacent."""
    raw = _WORKFLOW.read_text(encoding="utf-8")
    assert "OMN-16691" in raw, (
        "call-occ-autobind.yml must cite OMN-16691 inline so the carve-out is "
        "not read as an arbitrary variable rename"
    )
    assert "tail75df5e.ts.net" in raw, (
        "call-occ-autobind.yml must name the tailnet broker dependency inline — "
        "it is the entire reason this job cannot follow the shared seam"
    )
