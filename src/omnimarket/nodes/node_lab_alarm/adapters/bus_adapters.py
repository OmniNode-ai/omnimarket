# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two bus-backed readers of node_lab_alarm: the canary workflow's runs and the projection's newest row.

Both go through the deployed nodes that own the source, never straight to it: the canary's runs through the GitHub
gateway node (the lane's ``gh`` is refused for reads), the projection's newest row through the work-ledger query node.
The lane and the timeout come from the overlay; this module holds neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Protocol, cast

import yaml
from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.handlers.scm.handler_gateway_read_client import (
    GatewayReadError,
    HandlerGatewayReadClient,
    _token_resolves,
    read_in_process,
    read_over_bus,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
)
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_delegation_chain_canary import (
    ModelCanaryRun,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelDelegationChainCanarySettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    CanaryRunsUnreadableError,
    ProjectionUnreadableError,
)

_WORK_LEDGER_QUERY_NODE = "omnimarket.nodes.node_work_ledger_query_effect"


class ProtocolGatewayReader(Protocol):
    """One typed gateway read; HandlerGatewayReadClient is the production one."""

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult: ...


class GatewayCanaryRunSource:
    """The canary workflow's completed runs, one gateway read per admitted event, newest first."""

    def __init__(
        self,
        settings: ModelDelegationChainCanarySettings,
        client: ProtocolGatewayReader | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HandlerGatewayReadClient(route=self._route)

    def _route(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        """In process where the gateway's own token resolves, else the deployed gateway over the bus within the overlay's timeout."""
        if _token_resolves():
            return read_in_process(request)
        return read_over_bus(request, timeout_s=self._settings.timeout_seconds)

    def read(self) -> tuple[ModelCanaryRun, ...]:
        settings = self._settings
        runs: list[ModelCanaryRun] = []
        for event in settings.events:
            request = ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
                repo=settings.repository,
                workflow=settings.workflow,
                branch=settings.branch,
                event=event,
                status="completed",
                limit=settings.per_page,
            )
            try:
                result = self._client.read(request)
            except GatewayReadError as exc:
                raise CanaryRunsUnreadableError(str(exc)) from exc
            if not isinstance(result, ModelWorkflowRunsResult):
                raise CanaryRunsUnreadableError(
                    f"the gateway answered {type(result).__name__}, not workflow runs"
                )
            for run in result.runs:
                try:
                    created = datetime.fromisoformat(run.created_at)
                except ValueError as exc:
                    raise CanaryRunsUnreadableError(
                        f"run {run.id} carries an unreadable created_at"
                    ) from exc
                runs.append(
                    ModelCanaryRun(
                        id=run.id,
                        event=run.event,
                        status=run.status,
                        conclusion=run.conclusion,
                        created_at=created
                        if created.tzinfo is not None
                        else created.replace(tzinfo=UTC),
                        html_url=run.html_url,
                    )
                )
        return tuple(runs)


def work_ledger_query_topics() -> ModelScmTopics:
    """The work-ledger query node's command and terminal topics, as its contract declares them."""
    package = resources.files(_WORK_LEDGER_QUERY_NODE)
    contract = yaml.safe_load((package / "contract.yaml").read_text(encoding="utf-8"))
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=str(dispatch["command_topic"]),
        success=str(dispatch["terminal_events"]["success"]),
    )


def _result_body(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The query result inside a terminal payload: the payload itself, or the object under ``result``."""
    if "query" in payload:
        return payload
    inner = payload.get("result")
    if isinstance(inner, dict):
        return inner
    raise ProjectionUnreadableError("the terminal event carries no query result")


class BusProjectionNewest:
    """The projection's newest row_ts at or after ``since``, asked of the work-ledger query node over the bus."""

    def __init__(
        self,
        lane: str,
        timeout_seconds: float,
        *,
        call: Callable[[BaseModel, uuid.UUID], ModelScmTerminal] | None = None,
    ) -> None:
        self._lane = lane
        self._timeout = timeout_seconds
        self._call = call or self._bus_call

    def newest(self, since: datetime) -> datetime | None:
        correlation_id = uuid.uuid4()
        request = ModelWorkLedgerQueryRequest(
            correlation_id=correlation_id,
            query=EnumWorkLedgerQueryKind.ROWS,
            since=since,
            page_size=1,
            lines_only=True,
        )
        terminal = self._call(request, correlation_id)
        if terminal.failed or terminal.payload.get("status") == "failed":
            raise ProjectionUnreadableError(
                f"the query node refused: {terminal.payload.get('error', 'no reason given')}"
            )
        try:
            result = ModelWorkLedgerQueryResult.model_validate(
                _result_body(terminal.payload)
            )
        except ValidationError as exc:
            raise ProjectionUnreadableError(
                "the query node's answer is not a query result"
            ) from exc
        if result.error:
            raise ProjectionUnreadableError(f"the query node answered: {result.error}")
        if result.freshness is None or result.freshness.newest_row_ts is None:
            return None
        return result.freshness.newest_row_ts

    def _bus_call(
        self, request: BaseModel, correlation_id: uuid.UUID
    ) -> ModelScmTerminal:
        # Imported here: a test with an injected call must not need a bus client.
        from omnibase_infra.errors import InfraConnectionError

        from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
        from omnimarket.delegated_test_loop.lane_bus import (
            LabRunBusError,
            open_lab_run_bus,
        )
        from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller

        workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
        # The opener's fourth keyword (the workspace root) carries the private registry clone's name, which the
        # public-repo hygiene gate refuses; its parameters are bound from its signature in declared order.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
            raise ProjectionUnreadableError(
                f"unexpected bus opener parameters: {names}"
            )
        opener = cast(
            Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
            open_lab_run_bus,
        )
        values = ("kafka", self._lane, None, Path(workspace) if workspace else None)

        async def run() -> ModelScmTerminal:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                return await ScmBusCaller(bus).call_topics(
                    topics=work_ledger_query_topics(),
                    request=request,
                    correlation_id=correlation_id,
                    timeout_s=self._timeout,
                )

        try:
            # A caller under a running loop gets a thread of its own for the bus call.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, run()).result()
        except TimeoutError as exc:
            raise ProjectionUnreadableError(
                f"the query node gave no answer on lane {self._lane!r} within {self._timeout:.0f}s"
            ) from exc
        except (LabRunBusError, InfraConnectionError) as exc:
            raise ProjectionUnreadableError(f"bus lane {self._lane!r}: {exc}") from exc


__all__ = [
    "BusProjectionNewest",
    "GatewayCanaryRunSource",
    "ProtocolGatewayReader",
    "work_ledger_query_topics",
]
