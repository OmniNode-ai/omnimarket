# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the host drift owner dispatch: an open drift item past its bound gets one owned fix lane (OMN-24239).

The host drift checker raised its escalations as journal lines and, with no dispatch command set, started
nothing: ``ledger-writer:bus`` stayed open 17.7 days and ``env-file:host-env`` 6.6 days with the journal
saying ``dispatched=no``. This pass reads the checker's open items, renders each as the FRICTION row the alarm
owner responder reads, asks the responder which causes need a lane (no live lane covers them, past the policy's
bound, not dispatched inside its window), writes a brief per cause and starts one lane per cause through the
injected runner. A placement that failed is a lane entry that says so and is not recorded, so the next pass
retries it. Every value is the overlay's; the ports are injected.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.models.model_alarm_owner_responder import (
    ModelAlarmOwnerRequest,
)
from omnimarket.nodes.node_alarm_owner_responder_compute.handlers.handler_alarm_owner_responder import (
    HandlerAlarmOwnerResponder,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftLanePlaced,
    ModelDriftOpenItem,
    ModelDriftOwnerRequest,
    ModelDriftOwnerResult,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelAlarmCells,
    ModelBriefSections,
    ModelDriftOwnerOverlay,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    ProtocolBriefPreparer,
    ProtocolDispatchRecord,
    ProtocolDriftOpenItems,
    ProtocolLaneRunner,
    ProtocolLiveLanes,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def _cell(value: str, *, limit: int | None = None) -> str:
    """One ledger cell: no pipe, no line break, no run of spaces, cut at ``limit``."""
    text = " ".join(value.replace("|", " ").split())
    return text if limit is None else text[:limit]


def subject_of(item: ModelDriftOpenItem) -> str:
    return f"{item.host}:{item.key}"


def cause_key_of(item: ModelDriftOpenItem, alarm: ModelAlarmCells) -> str:
    """The key the responder dispatches on: the slugged condition and the subject (its own rule)."""
    return (
        f"{_NOT_SLUG.sub('-', alarm.condition.lower()).strip('-')}:{subject_of(item)}"
    )


def render_drift_row(item: ModelDriftOpenItem, alarm: ModelAlarmCells) -> str:
    """One FRICTION row for an open drift item, stamped with the moment the item was first seen.

    The cell grammar is the lab alarm's (``render_ledger_row`` of node_lab_alarm_deliver_effect): the alarm
    owner responder reads the stamp, ``actor``, ``condition``, ``subject`` and ``existing`` cells.
    """
    cells = [
        item.first_seen_at,
        "FRICTION",
        f"lane={alarm.lane}",
        f"actor={alarm.actor}",
        "model=none",
        f"class={alarm.friction_class}",
        f"condition={_cell(alarm.condition)}",
        f"subject={_cell(subject_of(item))}",
        f"raised_at={item.first_seen_at}",
        f"existing={alarm.ticket}",
        f"detail={_cell(item.kind)}",
        f"evidence={_cell(item.observed, limit=alarm.evidence_limit)}",
        f"host drift item {_cell(item.key)} open on {_cell(item.host)} since {item.first_seen_at}",
    ]
    return " | ".join(cells)


def render_brief(
    item: ModelDriftOpenItem,
    cause_key: str,
    lane: str,
    now: str,
    sections: ModelBriefSections,
) -> str:
    """The task part of a fix lane's brief: the cause, the host, the item, what the checker read and the overlay's lines.

    The brief preparer completes it into the whole brief (rules block, ticket text, 3a) and checks it.
    """
    return (
        "Host drift: an open item past its owner bound\n\n"
        f"Cause key: {cause_key}\n"
        f"Lane: {lane}\n"
        f"Host: {item.host}\n"
        f"Item: {item.key} ({item.kind})\n"
        f"Verdict: {item.verdict}\n"
        f"Declared: {item.declared}\n"
        f"Observed: {item.observed}\n"
        f"First seen: {item.first_seen_at}; read at {now}\n\n"
        "Find why the item drifted and fix the cause through a pull request in the repository that owns the "
        "declaration or the installer, then show the host drift check clear the item on that host. Cite "
        f"cause={cause_key} in your CLAIM. A cause that lives only on that host (uncommitted work, a login) is "
        "reported in your final message with the observed text as evidence, never worked around.\n\n"
        f"{sections.exemplars}\n{sections.seams}\n{sections.runtime}\n{sections.delegation}\n"
    )


class HandlerHostDriftOwnerDispatch:
    """``handle(request) -> result`` over the injected items, live lanes, dispatch record, briefs and runner."""

    def __init__(
        self,
        *,
        overlay: ModelDriftOwnerOverlay,
        items: ProtocolDriftOpenItems,
        live: ProtocolLiveLanes,
        record: ProtocolDispatchRecord,
        briefs: ProtocolBriefPreparer,
        runner: ProtocolLaneRunner,
        runner_path: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._items = items
        self._live = live
        self._record = record
        self._briefs = briefs
        self._runner = runner
        self._runner_path = runner_path
        self._clock = clock

    def handle(self, request: ModelDriftOwnerRequest) -> ModelDriftOwnerResult:
        overlay = self._overlay
        now = self._clock().astimezone(UTC).strftime(_STAMP)
        state = self._items.read()
        read = state.items
        excluded_kinds = frozenset(overlay.excluded_kinds)
        excluded_keys = frozenset(overlay.excluded_keys)
        kept = tuple(
            item
            for item in read
            if item.kind not in excluded_kinds and item.key not in excluded_keys
        )
        by_cause = {cause_key_of(item, overlay.alarm): item for item in kept}
        live = self._live.read(tuple(by_cause))
        answer = HandlerAlarmOwnerResponder().handle(
            ModelAlarmOwnerRequest(
                rows=tuple(render_drift_row(item, overlay.alarm) for item in kept),
                now=now,
                policy=overlay.policy,
                dispatched=self._record.load(),
                covered_causes=live.covered_causes,
                covered_lanes=live.covered_lanes,
            )
        )
        lanes: list[ModelDriftLanePlaced] = []
        recorded = self._record.load()
        for dispatch in answer.dispatches:
            item = by_cause[dispatch.cause_key]
            prepared = self._briefs.prepare(
                lane=dispatch.lane,
                name=f"{dispatch.lane}-brief.md",
                text=render_brief(
                    item, dispatch.cause_key, dispatch.lane, now, overlay.brief.sections
                ),
            )
            argv = self._argv(prepared.path, dispatch.lane, dispatch.cause_key)
            if not prepared.passed:
                lanes.append(
                    ModelDriftLanePlaced(
                        cause_key=dispatch.cause_key,
                        lane=dispatch.lane,
                        argv=argv,
                        brief_path=prepared.path,
                        brief_checked=False,
                        brief_sha256=prepared.sha256,
                        started=False,
                        dry_run=request.dry_run,
                        detail=f"brief-check-refused: {prepared.detail}",
                    )
                )
                continue
            if request.dry_run:
                lanes.append(
                    ModelDriftLanePlaced(
                        cause_key=dispatch.cause_key,
                        lane=dispatch.lane,
                        argv=argv,
                        brief_path=prepared.path,
                        brief_checked=True,
                        brief_sha256=prepared.sha256,
                        started=False,
                        dry_run=True,
                        detail="dry run: brief check passed, no lane started",
                    )
                )
                continue
            outcome = self._runner.place(argv)
            if outcome.started:
                recorded[dispatch.cause_key] = now
            lanes.append(
                ModelDriftLanePlaced(
                    cause_key=dispatch.cause_key,
                    lane=dispatch.lane,
                    argv=argv,
                    brief_path=prepared.path,
                    brief_checked=True,
                    brief_sha256=prepared.sha256,
                    started=outcome.started,
                    dry_run=False,
                    detail=outcome.detail,
                )
            )
        if not request.dry_run and any(lane.started for lane in lanes):
            self._record.save(recorded)
        return ModelDriftOwnerResult(
            host=state.host,
            items_read=len(read),
            rows_rendered=len(kept),
            lanes=tuple(lanes),
            skipped=answer.skipped,
            refusals=answer.refusals,
        )

    def _argv(self, brief_path: str, lane: str, cause_key: str) -> tuple[str, ...]:
        dispatch = self._overlay.dispatch
        return (
            dispatch.interpreter,
            self._runner_path,
            dispatch.subcommand,
            "--brief",
            brief_path,
            "--lane",
            lane,
            "--model",
            dispatch.model,
            "--ticket",
            dispatch.ticket,
            "--cause",
            cause_key,
            *dispatch.extra_args,
        )


__all__ = [
    "HandlerHostDriftOwnerDispatch",
    "cause_key_of",
    "render_brief",
    "render_drift_row",
    "subject_of",
]
