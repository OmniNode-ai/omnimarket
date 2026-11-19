# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Use the existing SCM gateway and work-ledger nodes on the declared bus."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from omnibase_infra.errors import InfraConnectionError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import (
    bound_lane_principal,
    open_lab_run_bus,
)
from omnimarket.handlers.bus_binding.select import open_bound_bus, positional_opener
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.handler_scm_local_state import default_watcher_state_path
from omnimarket.handlers.work_ledger_query_topics import work_ledger_query_topics
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerOpenClaim,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_red_main_alarm_effect.inventory import read_inventory
from omnimarket.nodes.node_red_main_alarm_effect.models import (
    ModelAlarmInventory,
    ModelAlarmSettings,
    ModelAlarmTarget,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

_TIMEOUT_SECONDS = 25.0
# A request ID is stable over a head's lifetime, including a return to an old head.
_QUERY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Match the throughput cause-claim read's one-day working window and
# 45-minute liveness rule, including its exception for held work leases.
_CLAIM_WINDOW = timedelta(days=1)
_CLAIM_QUIET_AFTER = timedelta(minutes=45)
_CLAIM_PAGE_SIZE = 100


def load_alarm_settings() -> ModelAlarmSettings:
    return load_structured_overlay(
        "node_red_main_alarm_effect",
        ModelAlarmSettings,
        file_env="ONEX_RED_MAIN_ALARM_OVERLAY",
    )


class AlarmBusPorts:
    def __init__(
        self,
        watcher: Path,
        settings: ModelAlarmSettings,
        bus: ProtocolLabRunBus,
        writer: WorkLedgerAppendCaller,
    ) -> None:
        self._watcher = watcher
        self._settings = settings
        self._calls = ScmBusCaller(bus)
        self._writer = writer
        self._claims_task: asyncio.Task[tuple[ModelWorkLedgerOpenClaim, ...]] | None = (
            None
        )
        self._append_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: ModelAlarmSettings) -> AsyncIterator[Self]:
        watcher = default_watcher_state_path()
        if watcher is None:
            raise ValueError("ONEX_PR_WATCHER_STATE does not name the PR watcher state")
        try:
            workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
            if not workspace:
                raise ValueError(
                    "OMNIBASE_PATH does not name the workspace for the bus lane"
                )
            async with open_bound_bus(
                open_bus=positional_opener(open_lab_run_bus),
                bus="kafka",
                lane=settings.bus_lane,
                kafka_bootstrap=None,
                workspace_root=Path(workspace),
            ) as bound:
                if bound.decision.degraded:
                    raise RuntimeError(
                        f"alarm bus binding is {bound.decision.state.value}"
                    )
                writer = WorkLedgerAppendCaller(
                    bound.bus, principal=bound_lane_principal()
                )
                try:
                    yield cls(watcher, settings, bound.bus, writer)
                finally:
                    await writer.stop()
        except InfraConnectionError as exc:
            raise RuntimeError("could not connect to the declared alarm bus") from exc

    def inventory(self, now: datetime) -> ModelAlarmInventory:
        return read_inventory(self._watcher, now, scm_owner=self._settings.scm_owner)

    async def check_runs(self, target: ModelAlarmTarget) -> ModelCommitCheckRunsResult:
        if len(target.required_checks) == 1:
            return await self._check_runs(target, target.required_checks[0])
        answer = await self._check_runs(target, None)
        if not answer.truncated:
            return answer
        # Large repos can have more check runs than the gateway's bound. Read
        # just the required contexts and keep a truncated result visibly unread.
        answers = [
            await self._check_runs(target, name) for name in target.required_checks
        ]
        return answer.model_copy(
            update={
                "total_count": sum(len(part.check_runs) for part in answers),
                "truncated": any(part.truncated for part in answers),
                "check_runs": [run for part in answers for run in part.check_runs],
            }
        )

    async def _check_runs(
        self, target: ModelAlarmTarget, name: str | None
    ) -> ModelCommitCheckRunsResult:
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
            repo=target.repo,
            head_sha=target.head_sha,
            check_name=name,
            limit=MAX_LIST_LIMIT,
        )
        terminal = await self._calls.call(
            node=EnumScmNode.GATEWAY,
            request=request,
            correlation_id=request.correlation_id,
            timeout_s=_TIMEOUT_SECONDS,
        )
        if terminal.failed or terminal.payload.get("status") == "failed":
            raise RuntimeError("SCM gateway refused the check-run request")
        answer = ModelCommitCheckRunsResult.model_validate(
            terminal.payload.get("result")
        )
        if (
            answer.repo.casefold() != target.repo.casefold()
            or answer.head_sha != target.head_sha
            or answer.check_name != name
        ):
            raise RuntimeError("SCM gateway answered a different check-run request")
        return answer

    async def _query(
        self, request: ModelWorkLedgerQueryRequest
    ) -> ModelWorkLedgerQueryResult:
        terminal = await self._calls.call_topics(
            topics=work_ledger_query_topics(),
            request=request,
            correlation_id=request.correlation_id,
            timeout_s=_TIMEOUT_SECONDS,
        )
        if terminal.failed or terminal.payload.get("status") == "failed":
            raise RuntimeError("work-ledger query failed")
        answer = ModelWorkLedgerQueryResult.model_validate(
            terminal.payload.get("result", terminal.payload)
        )
        if (
            answer.correlation_id != request.correlation_id
            or answer.query != request.query
        ):
            raise RuntimeError("work-ledger query answered a different request")
        if answer.error:
            raise RuntimeError(answer.error)
        return answer

    async def open_claims(self, now: datetime) -> tuple[ModelWorkLedgerOpenClaim, ...]:
        if self._claims_task is None:
            self._claims_task = asyncio.create_task(self._read_claims(now))
        return await self._claims_task

    async def _read_claims(self, now: datetime) -> tuple[ModelWorkLedgerOpenClaim, ...]:
        cursor = None
        claims_since = (
            (now - _CLAIM_WINDOW).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        page_size = _CLAIM_PAGE_SIZE
        claims: list[ModelWorkLedgerOpenClaim] = []
        while True:
            answer = await self._query(
                ModelWorkLedgerQueryRequest(
                    correlation_id=uuid4(),
                    query=EnumWorkLedgerQueryKind.OPEN_CLAIMS,
                    cursor=cursor,
                    now=now,
                    claims_since=claims_since,
                    page_size=page_size,
                )
            )
            if answer.refusal:
                if page_size == 1:
                    raise RuntimeError(
                        f"work-ledger open claims: {answer.refusal.code}"
                    )
                page_size = max(
                    1, min(page_size // 2, answer.refusal.suggested_limit or page_size)
                )
                continue
            if answer.watermark is None or answer.watermark < now - _CLAIM_QUIET_AFTER:
                raise RuntimeError(
                    "work-ledger open claims have no current read watermark"
                )
            claims.extend(
                claim
                for claim in answer.open_claims
                if claim.leases
                or datetime.fromisoformat(claim.newest_row_stamp)
                >= now - _CLAIM_QUIET_AFTER
            )
            if not answer.next_cursor:
                return tuple(claims)
            if answer.next_cursor == cursor:
                raise RuntimeError("work-ledger open claims returned the same page")
            cursor = answer.next_cursor

    async def recorded(self, request_id: UUID) -> bool:
        cell = f"req={request_id}"
        answer = await self._query(
            ModelWorkLedgerQueryRequest(
                correlation_id=uuid4(),
                query=EnumWorkLedgerQueryKind.ROWS,
                kinds=("STATUS", "MSG"),
                term=cell,
                since=_QUERY_EPOCH,
                page_size=3,
                lines_only=True,
            )
        )
        if answer.refusal:
            raise RuntimeError(f"work-ledger alarm lookup: {answer.refusal.code}")
        matching = [
            fields[1]
            for row in answer.lines
            if len(fields := [cell.strip() for cell in row.split("|")]) > 2
            and cell in fields[2:]
        ]
        if matching and sorted(matching) != ["MSG", "STATUS"]:
            raise RuntimeError("work ledger has an incomplete or repeated alarm pair")
        return bool(matching)

    async def append(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        # Caller.start subscribes once and must finish before another append
        # can reuse its terminal subscriptions.
        async with self._append_lock:
            return await self._writer.append(request, timeout_s=_TIMEOUT_SECONDS)
