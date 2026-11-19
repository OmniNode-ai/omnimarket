# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The owner step: an item with no running lane past its bound gets exactly one owner lane (OMN-25981, plan 3.3).

The pattern is the host drift owner dispatch's (node_host_drift_owner_dispatch_effect): the brief is completed and
checked through the lane-brief path, only a brief that passed goes to the remote-lane runner, and a placement that
failed is reported and retried by the next pass. Three things keep one lane per item:

* the live CLAIM: the owner lane's CLAIM carries ``cause=<cause_prefix>:<item id>`` (the runner's ``--cause``), and an
  item a live CLAIM names has a running lane (the host drift dispatch's covered-cause read);
* the ledger's own ``state=owned`` transition, appended after the lane started, in place of the host drift
  dispatch's dispatch-record file, so no store sits beside the ledger;
* the reservation of (item, revision, generation): the owner lane's name is that key's, and on the Temporal path the
  owner step runs as a child workflow whose id is that key, so a rerun, a second tick or a second worker cannot start
  it twice (the engine's workflow-id uniqueness, not a lease of ours).

The step assigns an owner and nothing else: every item is folded and every skip names a timing or placement fact.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    ProtocolBriefPreparer,
    ProtocolLaneRunner,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_item_fold import (
    fold_items,
    moment,
    stamp_of,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
    EnumItemState,
    EnumOwnerSkip,
    ModelItemOwnerRequest,
    ModelItemOwnerResult,
    ModelItemRecord,
    ModelOwnerDue,
    ModelOwnerPlaced,
    ModelOwnerPlan,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerOverlay,
    ModelItemOwnerPolicy,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.protocols import (
    PlanOwnerUnreadableError,
    ProtocolItemReservation,
    ProtocolLedgerRows,
    ProtocolPlanOwnerMissing,
    ProtocolRowAppender,
    RowAppendFailedError,
)


def _cell(value: str) -> str:
    return " ".join(value.replace("|", " ").split())


def owner_lane(
    policy: ModelItemOwnerPolicy, item: ModelItemRecord, generation: int
) -> str:
    return f"{policy.lane_prefix}-{item.item_id.split('-', 1)[1]}-g{generation}"


def reservation_key(item: ModelItemRecord, generation: int) -> str:
    return f"{item.item_id}-r{item.revision}-g{generation}"


def plan_owners(
    items: tuple[ModelItemRecord, ...], now: str, policy: ModelItemOwnerPolicy
) -> ModelOwnerPlan:
    """Pure: which items are due an owner lane at ``now``, oldest unowned first, at most ``lane_cap``."""
    at = moment(now)
    bound = timedelta(minutes=policy.owner_after_minutes)
    skipped: list[tuple[str, EnumOwnerSkip]] = []
    due: list[ModelItemRecord] = []
    for item in items:
        if item.state is EnumItemState.DONE:
            skipped.append((item.item_id, EnumOwnerSkip.DONE))
        elif item.running_lane is not None:
            skipped.append((item.item_id, EnumOwnerSkip.RUNNING))
        elif at - moment(item.unowned_since) < bound:
            skipped.append((item.item_id, EnumOwnerSkip.YOUNG))
        else:
            due.append(item)
    due.sort(key=lambda item: (item.unowned_since, item.item_id))
    for item in due[policy.lane_cap :]:
        skipped.append((item.item_id, EnumOwnerSkip.CAP))
    return ModelOwnerPlan(
        items=items,
        due=tuple(
            ModelOwnerDue(
                item=item,
                lane=owner_lane(policy, item, item.generation + 1),
                generation=item.generation + 1,
                reservation_key=reservation_key(item, item.generation + 1),
            )
            for item in due[: policy.lane_cap]
        ),
        skipped=tuple(skipped),
    )


def render_owned_row(
    policy: ModelItemOwnerPolicy, due: ModelOwnerDue, now: str, run_id: str, ticket: str
) -> str:
    """The plan 3.3 transition: ``state=owned owner=<lane>`` with the item revision and the run id."""
    item = due.item
    return " | ".join(
        [
            now,
            "STATUS",
            f"lane={policy.node_lane}",
            f"actor={policy.node_actor}",
            "model=none",
            f"ticket={ticket}",
            f"item={item.item_id}",
            "state=owned",
            f"owner={due.lane}",
            f"rev={item.revision}",
            f"run={_cell(run_id)}",
            f"gen={due.generation}",
            f"source={item.source.value}",
            f"owner lane {due.lane} placed for item {item.item_id} ({item.source.value}, "
            f"unowned since {item.unowned_since}; accountable owner {item.accountable_owner or 'none'})",
        ]
    )


def render_brief(
    due: ModelOwnerDue, policy: ModelItemOwnerPolicy, now: str, sections: str
) -> str:
    item = due.item
    return (
        "Owner lane: an item with no owner past its bound\n\n"
        f"Item: {item.item_id} (source {item.source.value}, revision {item.revision})\n"
        f"Lane: {due.lane} (binding {due.generation})\n"
        f"Source event: {item.source_ref}\n"
        f"Ticket: {item.ticket or 'none'}; PR: {item.pr or 'none'}\n"
        f"Accountable owner before this lane: {item.accountable_owner or 'none'}\n"
        f"First seen: {item.first_seen_at}; unowned since {item.unowned_since}; read at {now}\n"
        f"What the source said: {item.summary or 'nothing quoted'}\n\n"
        "You own this item. Read the source event and its lane, ticket or ruling on the ledger, find what is still "
        "undone, and do it through the sanctioned path for its kind, or record why it is already done with the "
        "evidence (a merged pull request, a TERMINAL, a RULING or a read-back receipt). "
        f"Cite cause={policy.cause_prefix}:{item.item_id} in your CLAIM. A blocked end is reported with its evidence; "
        "the next pass gives the item a new lane.\n\n"
        f"{sections}\n"
    )


class HandlerItemOwnerDispatch:
    """``handle(request) -> result`` over the injected rows, plan rules, reservation, briefs, runner and appender."""

    def __init__(
        self,
        *,
        overlay: ModelItemOwnerOverlay,
        rows: ProtocolLedgerRows,
        plan_owner: ProtocolPlanOwnerMissing,
        reservation: ProtocolItemReservation,
        briefs: ProtocolBriefPreparer,
        runner: ProtocolLaneRunner,
        appender: ProtocolRowAppender,
        runner_path: str,
        claude_dir: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._rows = rows
        self._plan_owner = plan_owner
        self._reservation = reservation
        self._briefs = briefs
        self._runner = runner
        self._appender = appender
        self._runner_path = runner_path
        self._claude_dir = claude_dir
        self._clock = clock

    def _now(self) -> str:
        return stamp_of(self._clock().astimezone(UTC))

    def _plan_rules(self) -> tuple[str, ...]:
        try:
            return self._plan_owner.read()
        except PlanOwnerUnreadableError:
            # Source c is read again next pass; the other three sources still get owners.
            return ()

    def plan(self) -> ModelOwnerPlan:
        """Read the rows and the plan rules, fold the items and name the due ones; raises LedgerUnreadableError."""
        now = self._now()
        policy = self._overlay.policy
        folded = fold_items(self._rows.read(), self._plan_rules(), now, policy)
        return plan_owners(folded.items, now, policy)

    def place(
        self, due: ModelOwnerDue, run_id: str, *, dry_run: bool = False
    ) -> ModelOwnerPlaced:
        """Re-read the item, and when it is still due at this generation, place its lane and write the owner on it."""
        current = {plan.item.item_id: plan for plan in self.plan().due}.get(
            due.item.item_id
        )
        if current is None or current.reservation_key != due.reservation_key:
            return ModelOwnerPlaced(
                item_id=due.item.item_id,
                lane=due.lane,
                generation=due.generation,
                started=False,
                skip=EnumOwnerSkip.RUNNING,
                detail="no longer due at this generation on the re-read",
            )
        overlay = self._overlay
        now = self._now()
        sections = overlay.brief.sections
        prepared = self._briefs.prepare(
            lane=due.lane,
            name=f"{due.lane}-brief.md",
            text=render_brief(
                due,
                overlay.policy,
                now,
                f"{sections.exemplars}\n{sections.seams}\n{sections.runtime}\n{sections.delegation}",
            ),
        )
        base = {
            "item_id": due.item.item_id,
            "lane": due.lane,
            "generation": due.generation,
            "brief_path": prepared.path,
            "brief_sha256": prepared.sha256,
        }
        if not prepared.passed:
            return ModelOwnerPlaced(
                **base,
                started=False,
                skip=EnumOwnerSkip.BRIEF_CHECK_FAILED,
                detail=prepared.detail,
            )
        if dry_run:
            return ModelOwnerPlaced(
                **base,
                started=False,
                skip=EnumOwnerSkip.DRY_RUN,
                detail=" ".join(self.argv(prepared.path, due)),
            )
        outcome = self._runner.place(self.argv(prepared.path, due))
        if not outcome.started:
            return ModelOwnerPlaced(
                **base,
                started=False,
                skip=EnumOwnerSkip.PLACEMENT_FAILED,
                detail=outcome.detail,
            )
        row = render_owned_row(
            overlay.policy, due, self._now(), run_id, overlay.dispatch.ticket
        )
        try:
            self._appender.append(row)
        except RowAppendFailedError as exc:
            # The lane runs and its CLAIM names the item's cause, so the next pass reads it as the running lane.
            return ModelOwnerPlaced(
                **base,
                started=True,
                detail=f"lane started; owner row not appended: {exc}",
            )
        return ModelOwnerPlaced(
            **base, started=True, owned_row=row, detail=outcome.detail
        )

    def handle(self, request: ModelItemOwnerRequest) -> ModelItemOwnerResult:
        plan = self.plan()
        placed: list[ModelOwnerPlaced] = []
        skipped = list(plan.skipped)
        for due in plan.due:
            if not self._reservation.reserve(due.reservation_key):
                skipped.append((due.item.item_id, EnumOwnerSkip.RESERVED))
                continue
            placed.append(self.place(due, request.run_id, dry_run=request.dry_run))
        return ModelItemOwnerResult(
            run_id=request.run_id,
            items=plan.items,
            placed=tuple(placed),
            skipped=tuple(skipped),
        )

    def argv(self, brief_path: str, due: ModelOwnerDue) -> tuple[str, ...]:
        dispatch = self._overlay.dispatch
        return (
            dispatch.interpreter,
            self._runner_path,
            dispatch.subcommand,
            "--brief",
            brief_path,
            "--lane",
            due.lane,
            "--model",
            dispatch.model,
            "--ticket",
            dispatch.ticket,
            "--cause",
            f"{self._overlay.policy.cause_prefix}:{due.item.item_id}",
            *(("--claude-dir", self._claude_dir) if self._claude_dir else ()),
            *(arg for plugin in dispatch.plugins for arg in ("--plugin", plugin)),
            *dispatch.extra_args,
        )


__all__ = [
    "HandlerItemOwnerDispatch",
    "owner_lane",
    "plan_owners",
    "render_brief",
    "render_owned_row",
    "reservation_key",
]
