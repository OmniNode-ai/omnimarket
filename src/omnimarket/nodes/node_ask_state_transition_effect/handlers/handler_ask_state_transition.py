# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append fenced STATUS rows through the ledger's atomic append_if_absent operation.

No lease or writer lives here. The Part A claim supplies exclusivity; the ledger
writer supplies ordering and cross-process row deduplication. A stale intent may
race an amendment or replacement, but cannot change that item's fold.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import yaml

from omnimarket.handlers.handler_operator_ledger import parse_ledger_row
from omnimarket.models.model_ask_state import ModelAskCompletionCheck, ModelAskEntry
from omnimarket.models.model_ask_state_transition import (
    ModelAskStateTransition,
    ModelAskStateTransitionRequest,
    ModelAskStateTransitionResult,
    ModelAskStateTransitionRuntime,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute
from omnimarket.nodes.node_ask_state_transition_effect.protocols import (
    ProtocolAskTransitionAppender,
)
from omnimarket.protocols.protocol_ask_completion_checker import (
    ProtocolAskCompletionChecker,
)


class OverlayLedgerAppender:
    """Resolve the node's bus overlay and use the existing ledger bus caller."""

    def __init__(self, runtime: ModelAskStateTransitionRuntime) -> None:
        self._runtime = runtime

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt:
        from omnimarket.delegated_test_loop.lane_bus import (
            bound_lane_principal,
            open_lab_run_bus,
        )
        from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
            resolve_node_event_bus,
        )
        from omnimarket.handlers.bus_binding.select import positional_opener
        from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

        root = os.environ.get(self._runtime.workspace_root_env)
        workspace = Path(root) if root else None
        selection, path = resolve_node_event_bus(
            "node_ask_state_transition_effect", workspace
        )
        if selection is None:
            raise RuntimeError(f"item transition event_bus overlay is absent: {path}")
        open_bus = positional_opener(open_lab_run_bus)
        async with open_bus(selection.type, selection.lane, None, workspace) as bus:
            caller = WorkLedgerAppendCaller(bus, principal=bound_lane_principal())
            try:
                return await caller.append(request, timeout_s=timeout_s)
            finally:
                await caller.stop()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_id(
    change: ModelAskStateTransition, request: ModelAskStateTransitionRequest
) -> str:
    # All five identity fields are included without truncation or delimiter ambiguity.
    identity = json.dumps(
        (change.item_id, change.state, change.evidence, change.revision, change.run_id),
        separators=(",", ":"),
    )
    return (
        request.snapshot.config.transition.row_id_prefix
        + "-"
        + hashlib.sha256(identity.encode()).hexdigest()
    )


def _from_ledger(
    request: ModelAskStateTransitionRequest, entries: tuple[ModelAskEntry, ...]
) -> tuple[ModelAskStateTransition, ...]:
    config = request.snapshot.config
    policy = config.transition
    changes: list[ModelAskStateTransition] = []
    parsed = [
        row
        for raw in request.snapshot.rows
        if (row := parse_ledger_row(raw)) is not None
    ]
    for entry in entries:
        if entry.claim_run_id is None:
            continue
        identities = {entry.item_id, entry.ask_id}
        claims = [
            row
            for row in parsed
            if row[1] == "CLAIM"
            and (row[3].get(policy.run_cell) or row[0]) == entry.claim_run_id
            and row[3].get(policy.revision_cell) == entry.revision
            and identities.intersection(
                ref
                for cell in config.ask_cells
                for ref in row[3].get(cell, "").split(",")
            )
        ]
        if not claims:
            continue
        binding = max(claims, key=lambda row: row[0])
        for stamp, kind, _, cells in sorted(parsed, key=lambda row: row[0]):
            if kind not in {"CLAIM", "TERMINAL"}:
                continue
            run = cells.get(policy.run_cell) or (
                stamp if kind == "CLAIM" else cells.get("closes-CLAIM")
            )
            if (
                run != entry.claim_run_id
                or cells.get(policy.revision_cell) != entry.revision
            ):
                continue
            if stamp < binding[0] or cells.get("lane") != binding[3].get("lane"):
                continue
            if kind == "TERMINAL" and cells.get("closes-CLAIM") not in {
                None,
                binding[0],
            }:
                continue
            refs = {
                ref
                for cell in config.ask_cells
                for ref in cells.get(cell, "").split(",")
            }
            # A TERMINAL can name only closes-CLAIM; the fold already brought it to its item.
            if kind == "CLAIM" and not identities.intersection(refs):
                continue
            state: Literal["owned", "parked", "done"] = "owned"
            evidence = f"{kind}:{stamp}"
            if kind == "TERMINAL":
                evidence = cells.get(policy.evidence_cell, "")
                if cells.get("outcome") in config.done_outcomes and any(
                    check.verified
                    and check.reference == evidence
                    and check.evidence_class in policy.verified_completion_classes
                    and check.revision == entry.revision
                    and check.run_id == run
                    for check in request.snapshot.completion_checks
                ):
                    state = "done"
            changes.append(
                ModelAskStateTransition(
                    item_id=entry.item_id,
                    state=state,
                    evidence=evidence,
                    revision=entry.revision,
                    run_id=run,
                    lane=cells.get("lane") or entry.owner_lane,
                    owner=entry.owner_lane,
                    at=datetime.fromisoformat(stamp.replace("Z", "+00:00")),
                )
            )
    return tuple(changes)


def _refusal(
    change: ModelAskStateTransition,
    entry: ModelAskEntry | None,
    request: ModelAskStateTransitionRequest,
) -> str | None:
    if (
        entry is None
        or entry.revision != change.revision
        or entry.claim_run_id != change.run_id
    ):
        return "stale item revision or CLAIM run"
    if change.at > request.snapshot.now or change.at < entry.said_at:
        return "transition outside snapshot time"
    if change.state == "parked" and (
        not change.reason or change.review_by is None or change.seq is None
    ):
        return "parked transition needs reason, review_by and seq"
    if change.state == "done" and not any(
        check.verified
        and check.reference == change.evidence
        and check.evidence_class
        in request.snapshot.config.transition.verified_completion_classes
        and check.revision == change.revision
        and check.run_id == change.run_id
        for check in request.snapshot.completion_checks
    ):
        return "done transition needs checked completion evidence"
    values = (
        change.item_id,
        change.evidence,
        change.revision,
        change.run_id,
        change.lane,
        change.owner,
        change.reason or "",
    )
    if any("|" in value or "\n" in value or "\r" in value for value in values):
        return "transition contains a ledger cell delimiter"
    return None


def _status_row(
    change: ModelAskStateTransition,
    request: ModelAskStateTransitionRequest,
    row_id: str,
) -> str:
    config = request.snapshot.config
    policy = config.transition
    cells = [
        f"{_stamp(change.at)}",
        "STATUS",
        f"lane={change.lane}",
        f"{policy.item_cell}={change.item_id}",
        f"{config.state_cell}={change.state}",
        f"{config.owner_cell}={change.owner}",
        f"{policy.evidence_cell}={change.evidence}",
        f"{policy.revision_cell}={change.revision}",
        f"{policy.run_cell}={change.run_id}",
        f"{policy.row_id_cell}={row_id}",
    ]
    if change.state == "parked":
        assert change.review_by is not None
        cells.extend(
            (
                f"{config.reason_cell}={change.reason}",
                f"{policy.review_by_cell}={_stamp(change.review_by)}",
                f"{config.seq_cell}={change.seq}",
            )
        )
    return " | ".join(cells)


class HandlerAskStateTransition:
    def __init__(
        self,
        appender: ProtocolAskTransitionAppender | None = None,
        *,
        runtime: ModelAskStateTransitionRuntime | None = None,
        completion_checker: ProtocolAskCompletionChecker | None = None,
    ) -> None:
        from omnimarket.handlers.handler_ask_completion_evidence import (
            OverlayCompletionChecker,
        )

        self._runtime = runtime or ModelAskStateTransitionRuntime.model_validate(
            yaml.safe_load(
                Path(__file__).parent.parent.joinpath("contract.yaml").read_text()
            )["runtime"]
        )
        self._appender = (
            appender if appender is not None else OverlayLedgerAppender(self._runtime)
        )
        self._completion_checker = (
            completion_checker
            if completion_checker is not None
            else (
                OverlayCompletionChecker(
                    self._runtime.workspace_root_env, "node_ask_state_transition_effect"
                )
                if appender is None
                else None
            )
        )

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, request: ModelAskStateTransitionRequest
    ) -> ModelAskStateTransitionResult:
        errors: list[str] = []
        if self._completion_checker is not None:
            checks: tuple[ModelAskCompletionCheck, ...] = ()
            try:
                checks = self._completion_checker.checks(
                    request.snapshot, request.transitions
                )
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                errors.append(
                    f"completion sources unreadable: {type(exc).__name__}: {exc}"
                )
            request = request.model_copy(
                update={
                    "snapshot": request.snapshot.model_copy(
                        update={"completion_checks": checks}
                    )
                }
            )
        fold = NodeAskStateCompute()
        report = fold.handle(request.snapshot)
        entries = {
            identity: entry
            for entry in report.asks
            for identity in (entry.item_id, entry.ask_id)
        }
        changes = request.transitions or _from_ledger(request, report.asks)
        rows: list[str] = []
        receipts = []
        skipped: list[str] = []
        new_rows: list[str] = []
        known_ids = {
            parsed[3].get(request.snapshot.config.transition.row_id_cell)
            for raw in request.snapshot.rows
            if (parsed := parse_ledger_row(raw)) is not None
        }
        for change in changes:
            entry = entries.get(change.item_id)
            reason = _refusal(change, entry, request)
            if reason:
                skipped.append(f"{change.item_id}: {reason}")
                continue
            assert entry is not None
            change = change.model_copy(update={"item_id": entry.item_id})
            row_id = _row_id(change, request)
            request_id = uuid5(NAMESPACE_URL, row_id)
            row = _status_row(change, request, row_id) + f" | req={request_id}"
            append = ModelWorkLedgerAppendRequest(
                request_id=request_id,
                ledger_id=self._runtime.ledger_id,
                rows=row,
                requested_by_lane=request.requested_by_lane,
                requesting_host=request.requesting_host,
                requested_at=request.snapshot.now,
                append_if_absent_row_id=row_id,
            )
            try:
                receipt = await self._appender.append(
                    append, timeout_s=self._runtime.append_timeout_seconds
                )
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                errors.append(f"{change.item_id}: {type(exc).__name__}: {exc}")
                continue
            receipts.append(receipt)
            if (
                receipt.request_id != request_id
                or receipt.exit_code != 0
                or receipt.status
                not in {
                    EnumWorkLedgerAppendStatus.ACCEPTED,
                    EnumWorkLedgerAppendStatus.DUPLICATE,
                    EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
                }
            ):
                errors.append(
                    f"{change.item_id}: ledger append refused: {receipt.message}"
                )
                continue
            rows.append(row)
            if row_id not in known_ids:
                new_rows.append(row)
                known_ids.add(row_id)
        state = fold.handle(
            request.snapshot.model_copy(
                update={"rows": (*request.snapshot.rows, *new_rows)}
            )
        )
        return ModelAskStateTransitionResult(
            rows=tuple(rows),
            receipts=tuple(receipts),
            skipped=tuple(skipped),
            errors=tuple(errors),
            state=state,
        )
