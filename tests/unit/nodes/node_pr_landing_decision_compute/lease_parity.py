# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure logic of the landing-decision parity gate (OMN-24484).

Kept free of pytest and of any runtime omnimarket import (the model is only
imported for typing) so both the autouse fixture in this directory's
``conftest.py`` and the unit tests in ``test_lease_parity_gate.py`` drive the
same rules.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
        ModelLandingDecision,
    )

PARITY_ENV: Final = "LANDING_LEASE_PARITY"
DEFAULT_MODE: Final = "compare"
GOLDEN_PATH: Final = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "pr_landing_decision"
    / "lease_lift_golden.json"
)


def parity_mode(environ: Mapping[str, str] = os.environ) -> str:
    """The active gate mode: ``capture``, ``off``, or ``compare`` (the default).

    Unset or blank is ``compare``, so CI always compares; ``capture`` and ``off``
    are the two explicit choices, and any other value also compares (the
    conftest runs everything that is not ``off`` or ``capture`` as a compare).
    """
    return environ.get(PARITY_ENV) or DEFAULT_MODE


def canonical(decision: ModelLandingDecision) -> str:
    """The decision as canonical JSON: sorted keys, no whitespace."""
    return json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def entry(decision: ModelLandingDecision) -> dict[str, Any]:
    """The captured fingerprint of one ``_decision`` call."""
    dispatched = [
        (
            action.cause_brief.brief_class.value
            if action.cause_brief is not None
            else action.brief.brief_class.value
            if action.brief is not None
            else ""
        )
        for action in decision.actions
        if action.kind.value == "dispatch_worker"
    ]
    return {
        "sha256": hashlib.sha256(canonical(decision).encode()).hexdigest(),
        "dispatched": dispatched,
        "recorded": len(decision.recorded_outcomes),
        "discarded": sum(
            1 for action in decision.actions if action.kind.value == "discard_result"
        ),
    }


def parity_verdict(
    nodeid: str,
    calls: list[dict[str, Any]],
    golden: dict[str, list[dict[str, Any]]],
) -> str | None:
    """``None`` when ``nodeid`` matches the pre-lift capture, else the failure.

    * a captured id: the sha-256 values of ``calls`` must equal the capture's,
      in order and in count.
    * an id the capture does not know, whose test FILE the capture does know
      (a test added to a file captured before the lift): failure -- there is
      no pre-lift baseline to compare against.
    * an id whose file the capture does not know at all (a file added after
      the capture): not a parity subject.
    """
    captured = golden.get(nodeid)
    if captured is not None:
        want = [one["sha256"] for one in captured]
        got = [one["sha256"] for one in calls]
        if len(got) != len(want):
            return (
                f"{nodeid}: {len(got)} decision call(s) after the lift, "
                f"{len(want)} captured before it"
            )
        if got != want:
            return f"{nodeid}: decisions differ from the pre-lift capture"
        return None
    test_file = nodeid.split("::", 1)[0]
    if any(one.split("::", 1)[0] == test_file for one in golden):
        return (
            f"{nodeid}: no pre-lift capture; capture on the pre-lift base "
            f"with {PARITY_ENV}=capture"
        )
    return None
