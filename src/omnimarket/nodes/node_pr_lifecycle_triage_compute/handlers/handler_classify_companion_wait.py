# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a red PR only waits on its open change-control companion (pure, definition-B).

On 2026-09-30, 28 of 86 landing workers returned ``external_blocker upstream_open`` on an open companion: a PR
whose only reds are the companion cascade (OCC Companion Merged Gate, occ-preflight eligibility, the CI Summary
that follows them) cannot go green before its companion merges, and no PR-level change helps (OMN-20140). Such a PR
is ``companion_wait``: ``{"companion", "companion_red"}``, where ``companion_red`` lists the companion's own reds at
its head (a companion red for its own reasons gets the worker instead). Anything not provably that (no companion, an
unknown or terminal one, a red read at an older head, a real red beside the cascade, a conflicting head) is None:
the PR keeps the old path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    SUMMARY_CHECK_RE,
    cascade_only,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_companion_wait_facts import (
    ModelCompanionWaitFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_companion_wait_verdict import (
    ModelCompanionWaitVerdict,
)

OCC_REPO = "onex_change_control"


def ci_of(rollup: str) -> str:
    """The head's CI from GitHub's rollup of every check-run and status on it (no flaky exception)."""
    r = (rollup or "").upper()
    if r == "SUCCESS":
        return "green"
    if r in ("FAILURE", "ERROR"):
        return "red"
    return "pending"


def merge_state_of(mergeable: str, merge_state: str) -> str:
    m, s = (mergeable or "").upper(), (merge_state or "").upper()
    if m == "CONFLICTING" or s == "DIRTY":
        return "conflicting"
    if s == "BEHIND":
        return "behind"
    if m == "MERGEABLE" and s in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return "clean"
    if s == "BLOCKED":
        return "blocked"
    return "unknown"


def companion_of(rec: Mapping[str, Any]) -> str | None:
    """The PR's change-control companion as ``onex_change_control#n``, from the watcher's record."""
    n = (rec.get("facts") or {}).get("evidence_companion")
    return f"{OCC_REPO}#{n}" if isinstance(n, int) and n > 0 else None


def state_of(short: str, records: Mapping[str, Mapping[str, Any]]) -> str:
    """A PR's state as the watcher last read it (``OPEN``, ``MERGED``, ``CLOSED``), or ``UNKNOWN``."""
    return str(
        ((records.get(short) or {}).get("facts") or {}).get("state") or "UNKNOWN"
    ).upper()


def own_reds(rec: Mapping[str, Any]) -> list[str]:
    """The red checks the watcher read at the PR's current head, the CI Summary aside."""
    ci = rec.get("ci") or {}
    head = str((rec.get("facts") or {}).get("head_sha") or "")
    if not head or ci.get("sha") != head:
        return []
    return sorted(
        str(n) for n in ci.get("red") or () if not SUMMARY_CHECK_RE.match(str(n))
    )


def companion_wait_of(
    short: str,
    rec: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    records: Mapping[str, Mapping[str, Any]],
    annotations: Mapping[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Whether a red PR only waits on its open change-control companion (OMN-20140); see the module doc."""
    if live is None or ci_of(str(live.get("rollup") or "")) != "red":
        return None
    if (
        merge_state_of(
            str(live.get("mergeable") or ""), str(live.get("merge_state") or "")
        )
        == "conflicting"
    ):
        return None
    ci = rec.get("ci") or {}
    if ci.get("sha") != str(live.get("head") or ""):
        return None
    if not cascade_only(ci.get("red") or (), ci.get("runs") or (), annotations):
        return None
    comp = companion_of(rec)
    if comp is None or comp == short or state_of(comp, records) != "OPEN":
        return None
    return {"companion": comp, "companion_red": own_reds(records.get(comp) or {})}


class HandlerClassifyCompanionWait:
    """Whether a red PR waits only on its open companion (pure, definition-B)."""

    def handle(self, request: ModelCompanionWaitFacts) -> ModelCompanionWaitVerdict:
        wait = companion_wait_of(
            request.short,
            request.rec.model_dump(exclude_unset=True),
            None
            if request.live is None
            else request.live.model_dump(exclude_unset=True),
            {
                short: rec.model_dump(exclude_unset=True)
                for short, rec in request.records.items()
            },
            request.annotations,
        )
        if wait is None:
            return ModelCompanionWaitVerdict(waiting=False)
        return ModelCompanionWaitVerdict(
            waiting=True,
            companion=wait["companion"],
            companion_red=tuple(wait["companion_red"]),
        )


__all__: list[str] = [
    "HandlerClassifyCompanionWait",
    "companion_of",
    "companion_wait_of",
    "own_reds",
    "state_of",
]
