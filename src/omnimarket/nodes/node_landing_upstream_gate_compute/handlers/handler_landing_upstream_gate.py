# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which external blockers hold one PR head from a fix worker (OMN-19998).

A worker that judges a PR ``external_blocker`` names the ref it waits on and ends. The controller then holds the PR at
a worker gate (``external:<ref>``) while that blocker still stands: a known kind, judged at this very head, naming
another PR or issue the PR watcher reads open. The gate used to end one hour after the judgement for every kind
(OMN-20864, ``WORKER_GATE_HOURS``), and the per-PR park (``retry_parks``) ended on the same hour, which cleared the
node's blocker fingerprint, so the node leased the next worker to a head whose blocker had not changed.

Observed controller state showed open upstream blockers being retried about 2.5 times an hour
because the gate expired after one hour. Under OMN-19998, an ``upstream_open`` head keeps its gate while its
upstream reads open, and it is lifted when the ref merges or closes. No clock ends it. A head move ends it too:
the blocker stands only at the head it was judged at, so a push is new work.

Every other known kind keeps the worker gate's backstop hours, as before. An upstream_open ref that names no PR or
issue, or the PR itself, never stands, and a ref the watcher does not read (``UNKNOWN``) holds nothing: the gate
fails open to a worker, never closed on a guess. Pure: it reads nothing and writes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_landing_upstream_gate_compute.models.model_landing_upstream_gate import (
    ModelLandingUpstreamGateRequest,
    ModelLandingUpstreamGateResult,
)

KNOWN_EXTERNAL_KINDS = frozenset(
    {
        "upstream_open",
        "operator_decision",
        "production_gate",
        "collaborator",
        "hold_row",
    }
)
UNBOUNDED_KINDS = frozenset({"upstream_open"})
REF_RE = re.compile(r"[A-Za-z0-9_.-]+#\d+")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _state(ref: str, records: Mapping[str, Mapping[str, Any]]) -> str:
    facts = (records.get(ref) or {}).get("facts")
    return (
        str((facts or {}).get("state") or "UNKNOWN").upper()
        if isinstance(facts, Mapping)
        else "UNKNOWN"
    )


def _hours_between(earlier: str, later: str) -> float | None:
    try:
        return (
            datetime.strptime(later, _STAMP) - datetime.strptime(earlier, _STAMP)
        ).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def upstream_gate(
    request: ModelLandingUpstreamGateRequest,
) -> ModelLandingUpstreamGateResult:
    """The blockers standing at ``request.head``, those with no backstop, and the ref the worker gate holds on."""
    standing: list[str] = []
    unbounded: list[str] = []
    gate_ref = ""
    for ref, judged in sorted(request.judged.items()):
        kind = str(judged.get("kind") or "")
        if (
            judged.get("head") != request.head
            or kind not in KNOWN_EXTERNAL_KINDS
            or not REF_RE.fullmatch(ref)
            or ref == request.pr
            or _state(ref, request.records) != "OPEN"
        ):
            continue
        standing.append(ref)
        if kind in UNBOUNDED_KINDS:
            unbounded.append(ref)
        age = _hours_between(str(judged.get("at") or ""), request.now)
        if not gate_ref and (
            kind in UNBOUNDED_KINDS or (age is not None and age < request.hours)
        ):
            gate_ref = ref
    return ModelLandingUpstreamGateResult(
        standing=tuple(standing), unbounded=tuple(unbounded), gate_ref=gate_ref
    )


class HandlerLandingUpstreamGate:
    def handle(
        self, request: ModelLandingUpstreamGateRequest
    ) -> ModelLandingUpstreamGateResult:
        return upstream_gate(request)
