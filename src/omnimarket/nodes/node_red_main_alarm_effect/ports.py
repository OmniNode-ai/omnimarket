# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Use the existing SCM gateway and work-ledger nodes on the declared bus."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from omnibase_infra.cli.delegate_lane_credentials import (
    ENV_SASL_PASSWORD,
    ENV_SASL_USERNAME,
)
from omnibase_infra.errors import InfraConnectionError, RuntimeHostError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import (
    LabRunBusError,
    bound_lane_principal,
    open_lab_run_bus,
)
from omnimarket.handlers.bus_binding.select import (
    BoundBus,
    open_bound_bus,
    positional_opener,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.handler_scm_local_state import default_watcher_state_path
from omnimarket.handlers.work_ledger_query_topics import work_ledger_query_topics
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
)
from omnimarket.models.overlay_documents.model_registry_documents import (
    ModelRepositoryRegistryOverlay,
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
    EnumAlarmFailure,
    ModelAlarmFailure,
    ModelAlarmInventory,
    ModelAlarmRepository,
    ModelAlarmSettings,
    ModelAlarmTarget,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

_TIMEOUT_SECONDS = 25.0
# The work-ledger query and append answer through the ledger host: the open-claims page
# and the alarm lookup were read at 25 s over the dev runtime's login, which is the
# whole gateway bound, so a ledger call has its own.
_LEDGER_TIMEOUT_SECONDS = 90.0
_CONFIG_ROOT_ENV = "ONEX_WORKSPACE_CONFIG_ROOT"
# The newest push runs on a default branch; the newest names the head the scan reads.
_HEAD_RUNS = 5
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


@contextlib.contextmanager
def _config_root_declared(settings: ModelAlarmSettings) -> Iterator[None]:
    """Name the overlay's mounted config root for the bus lane resolver while the lane binds.

    The dev runtime mounts the lane declaration but exports no ONEX_WORKSPACE_CONFIG_ROOT, so the
    resolver found no ci_bus_lanes.yaml and every fire answered a configuration failure before
    any gateway call. A root the environment already names wins; the variable is put back as it
    was once the bus is open, because only the open reads it.
    """
    declared = settings.workspace_config_root
    if declared is None or os.environ.get(_CONFIG_ROOT_ENV, "").strip():
        yield
        return
    os.environ[_CONFIG_ROOT_ENV] = declared
    try:
        yield
    finally:
        os.environ.pop(_CONFIG_ROOT_ENV, None)


def _writer_principal() -> str:
    """The broker login the ledger writer is named by.

    A lane that stores its login binds it with the lane transport. The runtime
    authenticates from KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD, the construction path
    the lane resolver steps aside for, so nothing is bound there and the login is
    that variable.
    """
    try:
        return bound_lane_principal()
    except LabRunBusError:
        username = os.environ.get(ENV_SASL_USERNAME, "").strip()
        if not username or not os.environ.get(ENV_SASL_PASSWORD, "").strip():
            raise
        return username


async def _open_durable_bus(
    stack: contextlib.AsyncExitStack, settings: ModelAlarmSettings, workspace: Path
) -> BoundBus:
    """The lane's durable bus, opened with the bound a runtime-hosted caller needs.

    The binding's own health read is the 8 s bound of a short-lived client command, and the node
    is not one: it runs inside the shared runtime, whose event loop stalls for seconds under load
    (log gaps of 3 to 7 s, consumer heartbeat sessions expiring), so the read reported a healthy
    broker degraded with ``TimeoutError`` while a fresh process opened the same lane in under a
    second. Each attempt waits the overlay's bound and a degraded one is closed and tried again; a
    binding still degraded after the last attempt is raised with the reason the decision holds,
    never as the bare state, so the failure the fire answers says why.
    """
    reasons: list[str] = []
    started = time.monotonic()
    for _ in range(settings.bus_open_attempts):
        async with contextlib.AsyncExitStack() as attempt:
            with _config_root_declared(settings):
                bound = await attempt.enter_async_context(
                    open_bound_bus(
                        open_bus=positional_opener(open_lab_run_bus),
                        bus="kafka",
                        lane=settings.bus_lane,
                        kafka_bootstrap=None,
                        workspace_root=workspace,
                        timeout_s=settings.bus_open_timeout_seconds,
                    )
                )
            if not bound.decision.degraded:
                stack.push_async_exit(attempt.pop_all())
                return bound
            reasons.append(bound.decision.reason.strip() or "no reason recorded")
    raise RuntimeError(
        f"alarm bus binding is {bound.decision.state.value}: {'; '.join(reasons)}"
        f" (attempts={len(reasons)}, health_read_s={settings.bus_open_timeout_seconds:g},"
        f" elapsed_s={time.monotonic() - started:.1f})"
    )


class AlarmBusPorts:
    def __init__(
        self,
        watcher: Path | None,
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
        # A declared repository map reads heads through the SCM gateway; only a scan with
        # no map needs the PR watcher snapshot.
        watcher = None if settings.repositories else default_watcher_state_path()
        if watcher is None and not settings.repositories:
            raise ValueError("ONEX_PR_WATCHER_STATE does not name the PR watcher state")
        try:
            workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
            if not workspace:
                raise ValueError(
                    "OMNIBASE_PATH does not name the workspace for the bus lane"
                )
            async with contextlib.AsyncExitStack() as stack:
                bound = await _open_durable_bus(stack, settings, Path(workspace))
                writer = WorkLedgerAppendCaller(
                    bound.bus, principal=_writer_principal()
                )
                try:
                    yield cls(watcher, settings, bound.bus, writer)
                finally:
                    await writer.stop()
        except InfraConnectionError as exc:
            raise RuntimeError("could not connect to the declared alarm bus") from exc

    async def inventory(self, now: datetime) -> ModelAlarmInventory:
        if self._watcher is not None:
            return read_inventory(
                self._watcher, now, scm_owner=self._settings.scm_owner
            )
        registry = load_structured_overlay(
            "repos_registry", ModelRepositoryRegistryOverlay
        )
        registered = {row.repo.casefold(): row.owner for row in registry.repositories}
        rows = await asyncio.gather(
            *(
                self._declared_target(entry, registered.get(entry.repo.casefold()))
                for entry in self._settings.repositories
            )
        )
        return ModelAlarmInventory(
            targets=tuple(row for row in rows if isinstance(row, ModelAlarmTarget)),
            failures=tuple(row for row in rows if isinstance(row, ModelAlarmFailure)),
        )

    async def _declared_target(
        self, entry: ModelAlarmRepository, registry_owner: str | None
    ) -> ModelAlarmTarget | ModelAlarmFailure:
        """The head of one declared branch: the newest push run's commit, read over the bus."""
        try:
            request = ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
                repo=entry.repo,
                branch=entry.branch,
                event="push",
                limit=_HEAD_RUNS,
            )
            terminal = await self._calls.call(
                node=EnumScmNode.GATEWAY,
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=_TIMEOUT_SECONDS,
            )
            if terminal.failed or terminal.payload.get("status") == "failed":
                raise RuntimeError("SCM gateway refused the workflow-run request")
            answer = ModelWorkflowRunsResult.model_validate(
                terminal.payload.get("result")
            )
            if answer.repo.casefold() != entry.repo.casefold():
                raise RuntimeError("SCM gateway answered a different repository")
            pushed = [run for run in answer.runs if run.head_branch == entry.branch]
            if not pushed:
                raise ValueError(f"no push run is on {entry.branch}")
            head = max(pushed, key=lambda run: run.created_at).head_sha
            return ModelAlarmTarget(
                repo=entry.repo,
                branch=entry.branch,
                head_sha=head,
                required_checks=entry.required_checks,
                owner=registry_owner,
                declared_owner=entry.owner,
            )
        except (OSError, RuntimeError, ValueError, RuntimeHostError) as exc:
            return ModelAlarmFailure(
                kind=EnumAlarmFailure.INVENTORY_UNREADABLE,
                repo=entry.repo,
                branch=entry.branch,
                detail=str(exc),
            )

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
            timeout_s=_LEDGER_TIMEOUT_SECONDS,
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
            return await self._writer.append(request, timeout_s=_LEDGER_TIMEOUT_SECONDS)
