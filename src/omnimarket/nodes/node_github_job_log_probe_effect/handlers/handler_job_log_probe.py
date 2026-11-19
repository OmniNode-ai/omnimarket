# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send one job-log command per due tick and record consecutive missed replies."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import Literal
from uuid import uuid5

from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.config.state_root import resolve_onex_state_root
from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_github_job_log_probe_effect.models import (
    NODE_NAME,
    ModelJobLogProbeReading,
    ModelJobLogProbeSettings,
    ModelJobLogProbeState,
    load_job_log_probe_settings,
)
from omnimarket.nodes.node_github_job_log_probe_effect.runtime_adapters import (
    append_runtime_ledger,
    read_runtime_gateway,
)

_LANE = "github-job-log-probe"
_TICKET = "OMN-25774"
_IO_ERRORS = (OSError, RuntimeError, InfraConnectionError)


class HandlerJobLogProbe:
    """The tick owns one read, and the ledger host owns the rows and their deduplication."""

    def __init__(
        self,
        *,
        settings: ModelJobLogProbeSettings | None = None,
        runtime_lane: str | None = None,
        state_path: Path | None = None,
        read: Callable[[ModelGithubGatewayRequest], Awaitable[ModelJobLogProbeReading]]
        | None = None,
        append: Callable[[ModelWorkLedgerAppendRequest], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._runtime_lane = runtime_lane
        self._state_path = state_path
        self._read = read
        self._append = append
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def _event(
        self,
        tick: ModelRuntimeTick,
        settings: ModelJobLogProbeSettings,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return ModelScheduleRunEvent(
            job_id=settings.job_id,
            node_name=NODE_NAME,
            tick_id=str(tick.tick_id),
            scheduled_for=tick.scheduled_at,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    async def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        if self._settings is None:
            try:
                self._settings = load_job_log_probe_settings()
            except NodeOverlayNotFoundError as exc:
                self._refusals.refuse(exc)
                return None
            self._refusals.recovered()
        settings = self._settings
        lane = self._runtime_lane or os.environ.get("ONEX_RUNTIME_LANE")
        if lane != settings.runtime_lane:
            return None
        path = self._state_path or (
            resolve_onex_state_root(purpose=NODE_NAME) / NODE_NAME / "state.json"
        )
        try:
            state = ModelJobLogProbeState.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            state = ModelJobLogProbeState()

        if state.pending is not None:
            try:
                await self._append_rows(state.pending, settings)
            except _IO_ERRORS as exc:
                return self._event(
                    request, settings, "failed", f"ledger_pending:{type(exc).__name__}"
                )
            state = state.model_copy(update={"pending": None})
            _save_state(path, state)

        now = self._clock()
        if (
            (
                state.last_scheduled_at is not None
                and request.scheduled_at <= state.last_scheduled_at
            )
            or (
                state.last_read_at is not None
                and now - state.last_read_at
                < timedelta(seconds=settings.run_interval_seconds)
            )
            or now - request.scheduled_at > timedelta(seconds=settings.grace_seconds)
        ):
            return None

        target = (settings.repo, settings.finished_job_id)
        gateway_request = ModelGithubGatewayRequest(
            correlation_id=uuid5(request.tick_id, NODE_NAME),
            operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
            repo=settings.repo,
            job_id=settings.finished_job_id,
            fill_on_miss=True,
            tail_bytes=1024,
        )
        started = monotonic()
        try:
            if self._read is None:
                reading = await read_runtime_gateway(
                    gateway_request, timeout_s=settings.timeout_seconds
                )
            else:
                reading = await self._read(gateway_request)
        except _IO_ERRORS as exc:
            reading = ModelJobLogProbeReading(
                verdict="unreadable", reason=type(exc).__name__
            )
        latency_ms = round((monotonic() - started) * 1000)

        previous_streak = state.no_terminal_streak if state.target == target else 0
        streak = min(2, previous_streak + 1) if reading.verdict == "no_terminal" else 0
        rows = _ledger_request(
            settings,
            gateway_request,
            reading,
            self._clock(),
            latency_ms,
            raise_alarm=previous_streak == 1 and streak == 2,
        )
        state = ModelJobLogProbeState(
            last_scheduled_at=request.scheduled_at,
            last_read_at=now,
            target=target,
            no_terminal_streak=streak,
            pending=rows,
        )
        _save_state(path, state)
        try:
            await self._append_rows(rows, settings)
        except _IO_ERRORS as exc:
            return self._event(
                request, settings, "failed", f"ledger_pending:{type(exc).__name__}"
            )
        _save_state(path, state.model_copy(update={"pending": None}))
        return self._event(
            request,
            settings,
            "completed" if reading.verdict == "log_returned" else "failed",
            f"{reading.verdict} reason={reading.reason} latency_ms={latency_ms}",
        )

    async def _append_rows(
        self, request: ModelWorkLedgerAppendRequest, settings: ModelJobLogProbeSettings
    ) -> None:
        if self._append is None:
            await append_runtime_ledger(
                request, timeout_s=settings.ledger_timeout_seconds
            )
        else:
            await self._append(request)


def _save_state(path: Path, state: ModelJobLogProbeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", dir=path.parent, encoding="utf-8", delete=False
    ) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(state.model_dump_json())
            temp.flush()
            os.fsync(temp.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _ledger_request(
    settings: ModelJobLogProbeSettings,
    request: ModelGithubGatewayRequest,
    reading: ModelJobLogProbeReading,
    observed: datetime,
    latency_ms: int,
    *,
    raise_alarm: bool,
) -> ModelWorkLedgerAppendRequest:
    stamp = observed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    actor = f"lane={_LANE} | actor=node:{NODE_NAME}"
    route = f"consumer_host={settings.consumer_host} | owner={settings.owner_lane}"
    target = f"repo={request.repo} | job_id={request.job_id}"
    reading_cells = (
        f"verdict={reading.verdict} | latency_ms={latency_ms} | reason={reading.reason}"
    )
    rows = (
        f"{stamp} | STATUS | {actor} | ticket={_TICKET} | {route} | {target}"
        f" | correlation_id={request.correlation_id} | {reading_cells} | Scheduled job-log read.\n"
    )
    if raise_alarm:
        rows += (
            f"{stamp} | FRICTION | {actor} | existing={_TICKET} | {route} | {target}"
            f" | correlation_id={request.correlation_id} | cost=~0 lane-minutes at raise"
            " | Two consecutive scheduled job-log reads returned no_terminal.\n"
        )
    return ModelWorkLedgerAppendRequest(
        request_id=request.correlation_id,
        rows=rows,
        requested_by_lane=_LANE,
        requesting_host=settings.consumer_host,
        requested_at=observed,
    )
