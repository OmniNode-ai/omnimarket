# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostIntrospection: assemble one host's announcement from its readings (OMN-19998).

Canonical def-B handler: ``handle(request: ModelHostIntrospectionRequest) -> ModelHostIntrospectionAnnouncement``.
Pure: the caller read the machine and the lab host table and passes the time and the announcement id, so the same
request always gives the same announcement. The lists come out in a fixed order and the digest covers the facts that
do not move on every beat.

Availability is the conjunction of the three things that say a host takes no work: it has no row in the lab host
table (the table is the one list placement reads), its row says it is unavailable, or its capacity reading carries a
refusal. The reason names each one that holds. A host that is not declared still announces: a new system coming
online introspects before anyone has declared it, and the registry decides what to do with it.

Two rows naming the same runtime, the same software of one kind, the same GPU slot or the same account make the
reading ambiguous and are refused, never merged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable

from omnimarket.models.model_host_introspection_announcement import (
    ModelHostAvailability,
    ModelHostIntrospectionAnnouncement,
    ModelHostLaneAccount,
    facts_digest,
)
from omnimarket.models.model_host_introspection_request import (
    ModelHostIntrospectionRequest,
)


class HostIntrospectionRefusedError(ValueError):
    """The readings are ambiguous or contradict the capacity advertisement; no announcement is made."""


def _refuse_duplicates(what: str, keys: Iterable[Hashable]) -> None:
    repeated = sorted(str(key) for key, count in Counter(keys).items() if count > 1)
    if repeated:
        raise HostIntrospectionRefusedError(
            f"{what} named more than once: {', '.join(repeated)}"
        )


class HandlerHostIntrospection:
    """COMPUTE handler: readings in, announcement out."""

    def handle(
        self, request: ModelHostIntrospectionRequest
    ) -> ModelHostIntrospectionAnnouncement:
        if request.capacity.host_name != request.host_name:
            raise HostIntrospectionRefusedError(
                f"capacity advertisement is for {request.capacity.host_name!r}, "
                f"the announcement is for {request.host_name!r}"
            )
        facts = request.facts
        _refuse_duplicates("runtime", (r.name for r in facts.runtimes))
        _refuse_duplicates("software", ((s.kind, s.name) for s in facts.software))
        _refuse_duplicates(
            "model served", ((m.name, m.port) for m in facts.models_served)
        )
        _refuse_duplicates("account", (a.account_name for a in request.lane_accounts))

        gpus = tuple(sorted(facts.gpus, key=lambda g: (g.name, g.mem_total_bytes)))
        models = tuple(sorted(facts.models_served, key=lambda m: (m.name, m.port)))
        runtimes = tuple(sorted(facts.runtimes, key=lambda r: r.name))
        software = tuple(sorted(facts.software, key=lambda s: (s.kind.value, s.name)))
        roles = tuple(sorted(set(request.row.roles)))
        advertised = set(request.capacity.tools)
        accounts = tuple(
            ModelHostLaneAccount(
                account_name=decl.account_name,
                can_run_lanes=decl.tool in advertised,
            )
            for decl in sorted(request.lane_accounts, key=lambda d: d.account_name)
        )
        availability = self._availability(request)
        digest = facts_digest(
            identity=facts.identity,
            roles=roles,
            cores=request.capacity.cores,
            mem_total_bytes=request.capacity.mem_total_bytes,
            gpus=gpus,
            models_served=models,
            runtimes=runtimes,
            software=software,
            lane_accounts=accounts,
            availability=availability,
        )
        return ModelHostIntrospectionAnnouncement(
            reason=request.reason,
            host_name=request.host_name,
            announced_at=request.announced_at,
            announcement_id=request.announcement_id,
            cadence_seconds=request.cadence_seconds,
            identity=facts.identity,
            roles=roles,
            capacity=request.capacity,
            gpus=gpus,
            models_served=models,
            runtimes=runtimes,
            software=software,
            lane_accounts=accounts,
            availability=availability,
            facts_digest=digest,
        )

    @staticmethod
    def _availability(request: ModelHostIntrospectionRequest) -> ModelHostAvailability:
        reasons: list[str] = []
        row = request.row
        if not row.declared:
            reasons.append("host has no row in the lab host table")
        elif not row.available:
            reasons.append(
                row.unavailable_reason or "row in the lab host table is unavailable"
            )
        if request.capacity.capacity_refusal:
            reasons.append(request.capacity.capacity_refusal)
        return ModelHostAvailability(available=not reasons, reason="; ".join(reasons))
