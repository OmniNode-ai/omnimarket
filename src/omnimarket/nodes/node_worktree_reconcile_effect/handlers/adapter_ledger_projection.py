# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection ledger source: reads work_ledger_rows through the work-ledger query node.

Every open claim inside the lookback window counts as live: only CLAIM, TERMINAL and
RELEASE rows are read, so a claim with no heartbeat of any other kind is still live.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel, JsonValue

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.rules_ledger_claims import (
    ModelLedgerRead,
    fold_ledger_lines,
)
from omnimarket.models.model_worktree_ledger_projection import (
    ModelLedgerProjectionSettings,
)
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_lab_alarm.protocols import ProjectionUnreadableError
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_replay import (
    kinds_complete,
)
from omnimarket.nodes.node_worktree_reconcile_effect.protocols import (
    ProtocolLedgerReader,
)

_SKEW_SECONDS = 3600


def _result_body(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The query result inside a terminal payload: the payload itself, or the object under ``result``."""
    if "query" in payload:
        return payload
    inner = payload.get("result")
    if isinstance(inner, dict):
        return inner
    raise ValueError("the terminal event carries no query result")


class ProjectionLedgerReader(ProtocolLedgerReader):
    """Reads the work-ledger projection through the work-ledger query node.

    The read is used only when the window's parity verdict shows every required row
    kind complete; anything short of that is an unread ledger, which keeps every tree.
    """

    def __init__(
        self,
        *,
        call: Callable[[BaseModel, uuid.UUID], ModelScmTerminal] | None = None,
    ) -> None:
        self._call = call

    def read(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> ModelLedgerRead | None:
        settings = command.ledger_projection
        assert settings is not None, "projection source requires ledger_projection"
        policy = command.policy
        call = self._call or self._default_call(settings)
        request = ModelWorkLedgerQueryRequest(
            correlation_id=uuid.uuid4(),
            query=EnumWorkLedgerQueryKind.ROWS,
            since=now - timedelta(days=policy.ledger_lookback_days),
            until=now,
            kinds=settings.required_kinds,
            page_size=settings.page_size,
        )
        lines: list[str] = []
        for page in range(settings.max_pages):
            try:
                terminal = call(request, request.correlation_id)
            except ProjectionUnreadableError as exc:
                return _unread(f"projection_unreadable:{type(exc).__name__}")
            if terminal.failed or terminal.payload.get("status") == "failed":
                return _unread("projection_error")
            try:
                result = ModelWorkLedgerQueryResult.model_validate(
                    _result_body(terminal.payload)
                )
            except ValueError:
                return _unread("projection_unreadable")
            if result.error or result.status != "ok" or result.refusal:
                return _unread("projection_error")
            if page == 0:
                reason = self._unproven(result, command, now)
                if reason:
                    return _unread(f"projection_{reason}")
            lines.extend(row.text for row in result.rows)
            if result.next_cursor is None:
                # Only CLAIM, TERMINAL and RELEASE rows are read, so no heartbeat of any
                # other kind is known: every open claim inside the window counts as live.
                return fold_ledger_lines(
                    lines, now, quiet_hours=policy.ledger_lookback_days * 24.0
                )
            request = request.model_copy(
                update={"cursor": result.next_cursor, "correlation_id": uuid.uuid4()}
            )
        return _unread("projection_too_large")

    @staticmethod
    def _unproven(
        result: ModelWorkLedgerQueryResult,
        command: ModelWorktreeReconcileCommand,
        now: datetime,
    ) -> str:
        """Why the window cannot vouch for "no lane holds this tree", or "" when its parity proves it."""
        settings = command.ledger_projection
        assert settings is not None
        freshness = result.freshness
        if freshness is None or freshness.newest_row_ts is None or result.matched == 0:
            return "empty"
        newest = freshness.newest_row_ts
        if (now - newest).total_seconds() > command.policy.ledger_max_age_hours * 3600:
            return "frozen"
        if (newest - now).total_seconds() > _SKEW_SECONDS:
            return "clock_skew"
        return kinds_complete(
            result.parity,
            settings.required_kinds,
            unmeasured_days_allowed=settings.unmeasured_days_allowed,
        )

    def _default_call(
        self, settings: ModelLedgerProjectionSettings
    ) -> Callable[[BaseModel, uuid.UUID], ModelScmTerminal]:
        from omnimarket.nodes.node_lab_alarm.adapters.bus_adapters import (
            BusProjectionNewest,
        )

        return BusProjectionNewest(settings.bus_lane, settings.timeout_seconds).ask


def _unread(reason: str) -> ModelLedgerRead:
    return ModelLedgerRead(fresh=False, reason=reason)
