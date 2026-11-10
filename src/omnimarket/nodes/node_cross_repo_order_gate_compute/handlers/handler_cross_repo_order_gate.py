# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold a change whose declared predecessor in another repository has not merged (OMN-23345).

The manifest repository change #1531 removed the intelligence-api entries of the lab lane manifest at 04:49Z; the half of the
same change that removes those containers from compose, omnibase_infra#5810, merged at 14:29Z. For those 9.6 hours
every omnibase_infra pull request failed ``test_lane_census_manifest_parity``, because the parity test reads the
manifest of the other repository. Nothing in either repository said that #1531 had to follow #5810, and the check that
could have said so (the parity test) lives in the repository that merged second.

A change split across repositories now says its order in its own body, one line per direction:

    Lands-After: omnibase_infra#5810
    Lands-Before: omnibase_infra#5811

``Lands-After`` lists the changes this pull request must follow; ``Lands-Before`` on a sibling's body makes that
sibling a predecessor of this pull request. The gate holds this pull request until every predecessor reads MERGED. A
predecessor that reads CLOSED without merging can never satisfy the order, a predecessor the watcher does not read
cannot be shown merged, and a declaration naming no ``repo#n`` is a typo that must not bypass the order: each of those
holds too, fail closed, because the author wrote the order down. A pull request that declares nothing, and has no
sibling declaring it, is allowed. Pure: it reads nothing and writes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.nodes.node_cross_repo_order_gate_compute.models.model_cross_repo_order_gate import (
    EnumCrossRepoOrderReason,
    EnumCrossRepoOrderVerdict,
    ModelCrossRepoOrderGateRequest,
    ModelCrossRepoOrderGateResult,
)

_AFTER_RE = re.compile(r"^[ \t]*lands-after[ \t]*:[ \t]*(?P<refs>.*)$", re.I | re.M)
_BEFORE_RE = re.compile(r"^[ \t]*lands-before[ \t]*:[ \t]*(?P<refs>.*)$", re.I | re.M)
_REF_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)?(?P<ref>[A-Za-z0-9_.-]+#\d+)")
_SPLIT_RE = re.compile(r"[,\s]+")


def _norm(ref: str) -> str:
    match = _REF_RE.fullmatch(ref.strip())
    return match.group("ref") if match else ""


def _declared(
    pattern: re.Pattern[str], body: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The refs a body names under one directive, and the tokens that named no ref."""
    refs: list[str] = []
    bad: list[str] = []
    for line in pattern.finditer(body):
        for token in _SPLIT_RE.split(line.group("refs").strip()):
            if not token:
                continue
            ref = _norm(token)
            if ref:
                refs.append(ref)
            else:
                bad.append(token)
    return tuple(dict.fromkeys(refs)), tuple(dict.fromkeys(bad))


def _state(ref: str, states: Mapping[str, str]) -> str:
    return str(states.get(ref) or "UNKNOWN").upper()


def order_gate(
    request: ModelCrossRepoOrderGateRequest,
) -> ModelCrossRepoOrderGateResult:
    """Allow ``request.pr`` only when every change it is declared to follow has merged."""
    pr = _norm(request.pr) or request.pr
    after, bad = _declared(_AFTER_RE, request.bodies.get(request.pr, ""))
    predecessors = [ref for ref in after if ref != pr]
    unparseable = list(bad)
    for sibling, body in sorted(request.bodies.items()):
        sibling_ref = _norm(sibling) or sibling
        if sibling_ref == pr:
            continue
        before, _ = _declared(_BEFORE_RE, body)
        if pr in before and sibling_ref not in predecessors:
            predecessors.append(sibling_ref)
    ordered = tuple(predecessors)
    if unparseable:
        return ModelCrossRepoOrderGateResult(
            verdict=EnumCrossRepoOrderVerdict.HOLD,
            reason=EnumCrossRepoOrderReason.DECLARATION_UNPARSEABLE,
            predecessors=ordered,
            waiting_on=ordered,
            unparseable=tuple(unparseable),
        )
    if not ordered:
        return ModelCrossRepoOrderGateResult(
            verdict=EnumCrossRepoOrderVerdict.ALLOW,
            reason=EnumCrossRepoOrderReason.NO_DECLARED_ORDER,
        )
    waiting = tuple(ref for ref in ordered if _state(ref, request.states) != "MERGED")
    if not waiting:
        return ModelCrossRepoOrderGateResult(
            verdict=EnumCrossRepoOrderVerdict.ALLOW,
            reason=EnumCrossRepoOrderReason.PREDECESSORS_MERGED,
            predecessors=ordered,
        )
    states = {_state(ref, request.states) for ref in waiting}
    reason = (
        EnumCrossRepoOrderReason.PREDECESSOR_CLOSED
        if "CLOSED" in states
        else EnumCrossRepoOrderReason.PREDECESSOR_UNREAD
        if "UNKNOWN" in states
        else EnumCrossRepoOrderReason.PREDECESSOR_OPEN
    )
    return ModelCrossRepoOrderGateResult(
        verdict=EnumCrossRepoOrderVerdict.HOLD,
        reason=reason,
        predecessors=ordered,
        waiting_on=waiting,
    )


class HandlerCrossRepoOrderGate:
    def handle(
        self, request: ModelCrossRepoOrderGateRequest
    ) -> ModelCrossRepoOrderGateResult:
        return order_gate(request)
