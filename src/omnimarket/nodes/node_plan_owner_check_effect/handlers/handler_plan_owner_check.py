# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OWNED, MISSING or UNKNOWN for one plan finding from the bus-fed ledger open-claims answer.

The hourly throughput tick's question, "does a live lane CLAIM name this plan?", is answered
here from the lines of ``node_work_ledger_reader_effect`` (the query node's ``open-claims``
point over the projection, OMN-24445). A claim owns the plan when its ``ticket=`` is one of the
rule's tickets, its ``plan=`` cell names one of the rule's plan paths, or its ``pr=`` names one of
the rule's pull requests, and its newest row is within the rule's live window; a row in the
future counts as live. The lane's name never decides, and a closed claim (a TERMINAL ended it)
is not in the open-claims answer, so it owns nothing. OWNED answers even when
the read is stale. MISSING needs a read that answered fresh and no owning claim: a failed
read, a stale answer with no owner and a claim line this handler cannot read are UNKNOWN,
never MISSING.

Every verdict emits one event: ``answered`` (golden) for OWNED and MISSING, ``refused``
(error) for UNKNOWN. Named break: ``_is_live`` always true reads a claim past its window as
an owner.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    EnumReaderStatus,
)
from omnimarket.models.plan_owner_check import (
    EnumPlanOwnerVerdict,
    ModelPlanOwnerEvent,
    ModelPlanOwnerOverlay,
    ModelPlanOwnerRequest,
    ModelPlanOwnerRule,
    ModelPlanOwnerVerdict,
)
from omnimarket.nodes.node_plan_owner_check_effect.protocols import (
    ProtocolPlanOwnerEventSink,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

# An OPEN-CLAIM line of the query node: the cells this decision reads. The line's shape is
# work_ledger_state_answer._claim_lines: ``OPEN-CLAIM lane=<lane> claim=<stamp> db:<row id>
# ticket=<ticket|none> pr=<pr|none> plan=<plan|none> newest-row=<stamp> kind=<kind> age=<age> rows=<n> host=<host|none>
# run=<run|none> leases=<list|none> liveness=unknown``. The answer's first line is the
# ``open-claims since=`` heading, which does not start with ``OPEN-CLAIM `` and is ignored.
# ``pr=`` and ``plan=`` are the CLAIM row's own cells (``none`` when it has none); a line without
# them is the shape of a reader that predates them and is UNKNOWN, never MISSING.
_CLAIM = re.compile(
    r"^OPEN-CLAIM lane=(?P<lane>\S+) claim=\S+ db:\S+ ticket=(?P<ticket>\S+) "
    r"pr=(?P<pr>\S+) plan=(?P<plan>\S+) newest-row=(?P<newest>\S+)"
)
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _stamp(text: str) -> datetime | None:
    """A ledger stamp (``2026-11-14T14:42:29Z``) as a UTC moment, or None."""
    if not _STAMP.match(text):
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _plan_key(path: str) -> str:
    """A plan path by its file stem: no directories, no ``.md``."""
    name = path.strip().rsplit("/", 1)[-1]
    return name.removesuffix(".md")


def _pr_key(pr: str) -> str:
    """A pull request as ``<repo>#<n>``, whether or not the owner is named."""
    return pr.strip().rsplit("/", 1)[-1]


def _names_plan(rule: ModelPlanOwnerRule, ticket: str, pr: str, plan: str) -> bool:
    """Whether the claim's own cells name the rule's plan: a listed ticket, plan path or pull request."""
    plan_keys = {_plan_key(p) for p in rule.plan_paths}
    claimed_plans = {_plan_key(p) for p in plan.split(",") if p.strip()}
    return (
        ticket in rule.tickets
        or _pr_key(pr) in {_pr_key(p) for p in rule.prs}
        or bool(plan_keys & claimed_plans)
    )


def _is_live(newest: datetime, now: datetime, window_minutes: int) -> bool:
    """A claim is live while its newest row is inside the rule's window; a row in the future is live."""
    return now - newest <= timedelta(minutes=window_minutes)


class HandlerPlanOwnerCheck:
    """``handle(ModelPlanOwnerRequest) -> ModelPlanOwnerVerdict``."""

    def __init__(
        self,
        overlay: ModelPlanOwnerOverlay,
        events: ProtocolPlanOwnerEventSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        topics: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._events = events
        self._clock = clock
        self._topics = dict(load_topics() if topics is None else topics)

    def handle(self, request: ModelPlanOwnerRequest) -> ModelPlanOwnerVerdict:
        rule = next(
            (r for r in self._overlay.rules if r.finding == request.finding), None
        )
        if rule is None:
            return self._finish(
                request, EnumPlanOwnerVerdict.UNKNOWN, "no-rule-for-finding"
            )
        answer = request.node_answer
        if answer.status not in (EnumReaderStatus.OK, EnumReaderStatus.STALE):
            return self._finish(
                request,
                EnumPlanOwnerVerdict.UNKNOWN,
                f"node-read-failed:{answer.reason}",
                fix=rule.unknown_fix,
            )
        now = self._clock().astimezone(UTC)
        owners: list[str] = []
        for line in answer.lines:
            if not line.startswith("OPEN-CLAIM "):
                continue
            claim = _CLAIM.match(line)
            newest = _stamp(claim["newest"]) if claim else None
            if claim is None or newest is None:
                return self._finish(
                    request,
                    EnumPlanOwnerVerdict.UNKNOWN,
                    "unparseable-claim-line",
                    fix=rule.unknown_fix,
                )
            if _names_plan(
                rule, claim["ticket"], claim["pr"], claim["plan"]
            ) and _is_live(newest, now, rule.live_window_minutes):
                owners.append(claim["lane"])
        if owners:
            return self._finish(
                request,
                EnumPlanOwnerVerdict.OWNED,
                "live-claim-names-the-plan",
                tuple(sorted(set(owners))),
            )
        if answer.status is EnumReaderStatus.STALE:
            return self._finish(
                request,
                EnumPlanOwnerVerdict.UNKNOWN,
                "watermark-stale-and-no-owner",
                fix=rule.unknown_fix,
            )
        return self._finish(
            request,
            EnumPlanOwnerVerdict.MISSING,
            "no-live-claim-names-the-plan",
            why=rule.why,
            fix=rule.fix,
        )

    def _finish(
        self,
        request: ModelPlanOwnerRequest,
        verdict: EnumPlanOwnerVerdict,
        reason: str,
        owners: tuple[str, ...] = (),
        *,
        why: str = "",
        fix: str = "",
    ) -> ModelPlanOwnerVerdict:
        golden = verdict is not EnumPlanOwnerVerdict.UNKNOWN
        event = ModelPlanOwnerEvent(
            kind=EnumReaderEventKind.ANSWERED
            if golden
            else EnumReaderEventKind.REFUSED,
            topic=self._topics[
                "plan_owner_answered" if golden else "plan_owner_refused"
            ],
            finding=request.finding,
            verdict=verdict,
            reason=reason,
            at=self._clock().astimezone(UTC),
            owners=owners,
        )
        self._events.emit(event)
        return ModelPlanOwnerVerdict(
            finding=request.finding,
            verdict=verdict,
            reason=reason,
            owners=owners,
            why=why,
            fix=fix,
            event=event,
        )


__all__ = ["HandlerPlanOwnerCheck"]
