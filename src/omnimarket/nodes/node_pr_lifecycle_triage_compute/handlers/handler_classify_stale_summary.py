# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a head's red is only a CI Summary left red by cancelled or skipped siblings (pure, definition-B).

Stale summary: ``CI Summary`` is red, nothing else failed or is still running, and a sibling was cancelled or
skipped (omnimarket#3423, #3424, omniclaude#2551 landed 4 to 25 minutes after a failed-jobs rerun). One failed-jobs
rerun per head, then one update-branch per head; a skipped matrix row whose name was never expanded
(omnibase_infra#4576) clears only on a fresh head, so it starts at the update-branch, then the one rerun when that
left the head unchanged (omnibase_infra#4745, OMN-20413). Update-branches are counted per PR, so new heads cannot loop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    SUMMARY_CHECK_RE,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_stale_summary_kind import (
    EnumStaleSummaryKind,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_summary_facts import (
    ModelStaleSummaryFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_summary_verdict import (
    ModelStaleSummaryVerdict,
)

UNEXPANDED_RE = re.compile(r"\$\{\{")
STALE_SIBLING = frozenset({"cancelled", "skipped"})
FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)
# A provider incident cancels (or leaves without a runner) dozens of a head's jobs: a rerun can land inside the
# incident, an update-branch gives a fresh head. This many distinct such checks marks the head as one.
INCIDENT_CONCLUSIONS = frozenset({"cancelled", "startup_failure"})
INCIDENT_MIN_RUNS = 12


def _newest_copies(runs: Iterable[Any]) -> dict[str, list[tuple[str, str]]]:
    """Per check name, the ``(status, conclusion)`` of its copies at the newest stamp (several on a tie)."""
    rows: dict[str, list[tuple[str, str, str]]] = {}
    for r in runs:
        if isinstance(r, (list, tuple)) and len(r) >= 3:
            rows.setdefault(str(r[0]), []).append(
                (
                    str(r[1] or "").lower(),
                    str(r[2] or "").lower(),
                    str((r[3] if len(r) > 3 else "") or ""),
                )
            )
    out: dict[str, list[tuple[str, str]]] = {}
    for name, copies in rows.items():
        newest = max(c[2] for c in copies)
        out[name] = [(s, c) for s, c, t in copies if t == newest]
    return out


def stale_summary_of(ci: Mapping[str, Any], head: str) -> str | None:
    """``rerun``, ``refresh``, ``refresh_first`` or None: whether this head's red is only a CI Summary left red by
    cancelled or skipped siblings, read from the watcher record at ``head`` (any other head is never guessed at).
    ``refresh_first``: ``INCIDENT_MIN_RUNS`` or more checks were cancelled or had no runner, a provider incident
    the one rerun can itself land inside, so the update-branch goes first and the rerun is kept behind it."""
    if not head or ci.get("sha") != head or ci.get("pending"):
        return None
    red = [str(n) for n in ci.get("red") or ()]
    if not any(SUMMARY_CHECK_RE.match(n) for n in red):
        return None
    copies = _newest_copies(ci.get("runs") or ())
    others = {n: cs for n, cs in copies.items() if not SUMMARY_CHECK_RE.match(n)}
    if any(s != "completed" for cs in others.values() for s, _c in cs):
        return None
    hit = [
        n for n, cs in copies.items() if any(c in INCIDENT_CONCLUSIONS for _s, c in cs)
    ]  # the Summary counts
    incident = len(hit) >= INCIDENT_MIN_RUNS
    stale_conclusions = (
        STALE_SIBLING | INCIDENT_CONCLUSIONS if incident else STALE_SIBLING
    )
    if any(
        c in FAILED_CONCLUSIONS and c not in stale_conclusions
        for cs in others.values()
        for _s, c in cs
    ):
        return None
    for n in red:
        if not SUMMARY_CHECK_RE.match(n) and not (
            n in others and all(c in stale_conclusions for _s, c in others[n])
        ):
            return None
    stale = [
        n for n, cs in others.items() if any(c in stale_conclusions for _s, c in cs)
    ]
    if not stale:
        return None
    if any(UNEXPANDED_RE.search(n) for n in stale):
        return "refresh"
    return "refresh_first" if incident else "rerun"


class HandlerClassifyStaleSummary:
    """Whether the head's red is a stale CI Summary, and which remedy goes first (pure, definition-B)."""

    def handle(self, request: ModelStaleSummaryFacts) -> ModelStaleSummaryVerdict:
        kind = stale_summary_of(request.ci.model_dump(exclude_unset=True), request.head)
        return ModelStaleSummaryVerdict(
            kind=None if kind is None else EnumStaleSummaryKind(kind)
        )


__all__: list[str] = ["HandlerClassifyStaleSummary", "stale_summary_of"]
