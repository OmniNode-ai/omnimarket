# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill's seam to the WIP admission.

The fill asks ``WipFillGate.refusal(item)`` once per candidate. A candidate whose kind the
overlay lists as fix-forward is never refused; every other candidate is new work and is
refused while the started-but-unlanded count is at its limit. An overlay that cannot be
read is not a count of zero and not a block: the gate reports why (``unread``) and judges
nothing, so the fill's caller prints the finding.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_wip_admission import (
    build_wip_read,
    decide_wip_admission,
)
from omnimarket.models.wip_admission import (
    EnumWipOutcome,
    EnumWipWorkKind,
    ModelWipAdmissionConfig,
    ModelWipAdmissionRequest,
    ModelWipClaim,
    ModelWipPolicy,
    ModelWipRead,
)
from omnimarket.nodes.node_wip_admission_compute.overlay import (
    load_wip_admission_config,
    load_wip_admission_policy,
)


@dataclass(frozen=True)
class WipFillGate:
    policy: ModelWipPolicy | None
    config: ModelWipAdmissionConfig
    read: ModelWipRead
    now: datetime
    unread: str = ""

    def refusal(self, item: Mapping[str, Any]) -> str | None:
        """Why the fill must not start this candidate, or ``None`` to go on."""
        if (
            self.policy is None
            or str(item.get("kind")) in self.policy.fix_forward_kinds
        ):
            return None
        decision = decide_wip_admission(
            ModelWipAdmissionRequest(
                kind=EnumWipWorkKind.NEW_WORK,
                repo=str(item.get("repo") or ""),
                ticket=str(item.get("ticket") or ""),
                now=self.now,
                read=self.read,
                policy=self.policy,
                config=self.config,
            )
        )
        if decision.outcome is EnumWipOutcome.ADMIT:
            return None
        return f"wip-limit: {decision.line}"


def wip_fill_gate(
    *,
    watcher_state: Mapping[str, Any] | None,
    claimed_tickets: Collection[str],
    now: datetime,
    environ: Mapping[str, str] | None = None,
) -> WipFillGate:
    """The fill's gate from the facts it holds; a live CLAIM stands for a started ticket."""
    config = load_wip_admission_config()
    read = build_wip_read(
        pr_state=watcher_state,
        claims=[ModelWipClaim(lane="", ticket=t) for t in sorted(claimed_tickets)],
        ticket_states=None,
        config=config,
    )
    try:
        policy = load_wip_admission_policy(environ)
    except NodeOverlayError as exc:
        return WipFillGate(None, config, read, now, unread=str(exc)[:200])
    return WipFillGate(policy, config, read, now)


def fill_wip_gate(
    *,
    ledger_tickets: Callable[[], tuple[set[str], dict[str, str]]] | None,
    watcher_state: Mapping[str, Any] | None,
    now: datetime,
    environ: Mapping[str, str] | None = None,
) -> tuple[Callable[[Mapping[str, Any]], str | None], tuple[str, str, str, str] | None]:
    """The fill's refusal function and, when the gate could not judge, the finding to print.

    ``ledger_tickets`` answers the tickets a live CLAIM names and raises when the ledger
    cannot be read; an unread ledger makes the read unreadable, which refuses new work only.
    """
    claim_error = ""
    claimed: set[str] = set()
    if ledger_tickets is not None:
        try:
            claimed = set(ledger_tickets()[0])
        except Exception as exc:
            claim_error = f"{type(exc).__name__}: {exc}"[:200]
    gate = wip_fill_gate(
        watcher_state=watcher_state, claimed_tickets=claimed, now=now, environ=environ
    )
    if claim_error:
        gate = replace(
            gate, read=ModelWipRead(readable=False, unreadable_reason=claim_error)
        )
    if gate.unread:
        return gate.refusal, (
            "unknown",
            "lab-fill-wip",
            f"the wip_admission overlay is unreadable, so the fill judges no work-in-progress limit: {gate.unread}",
            "supply the wip_admission overlay under ONEX_SKILL_OVERLAY_ROOTS",
        )
    if claim_error:
        return gate.refusal, (
            "unknown",
            "lab-fill-wip",
            f"the live CLAIMs are unreadable, so the fill starts no new work: {claim_error}",
            "restore the declared ledger read",
        )
    return gate.refusal, None
