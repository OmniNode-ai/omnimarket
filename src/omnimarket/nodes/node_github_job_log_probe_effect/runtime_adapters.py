# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read and append on the deployed runtime's authenticated event bus."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.kafka_auth import build_confluent_auth_config
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.handlers.scm.handler_scm_consumer_diagnostics import ScmNoTerminalError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.model_github_gateway_io import (
    ModelGithubGatewayRequest,
    ModelJobLogTailResult,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_github_job_log_probe_effect.models import (
    ModelJobLogProbeReading,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller


async def read_gateway(
    bus: ProtocolLabRunBus,
    request: ModelGithubGatewayRequest,
    *,
    timeout_s: float,
) -> ModelJobLogProbeReading:
    try:
        terminal = await ScmBusCaller(bus).call(
            node=EnumScmNode.GATEWAY,
            request=request,
            correlation_id=request.correlation_id,
            timeout_s=timeout_s,
        )
    except ScmNoTerminalError as exc:
        return ModelJobLogProbeReading(
            verdict="no_terminal", reason=exc.evidence.reason
        )

    if terminal.failed or terminal.payload.get("status") == "failed":
        return ModelJobLogProbeReading(
            verdict="gateway_failed", reason="failed_terminal"
        )
    try:
        result = ModelJobLogTailResult.model_validate(terminal.payload.get("result"))
    except ValidationError:
        return ModelJobLogProbeReading(
            verdict="gateway_failed", reason="invalid_terminal"
        )
    if (result.repo, result.job_id) != (request.repo, request.job_id):
        return ModelJobLogProbeReading(
            verdict="gateway_failed", reason="mismatched_job"
        )
    if not result.text:
        return ModelJobLogProbeReading(verdict="gateway_failed", reason="empty_log")
    return ModelJobLogProbeReading(verdict="WORKING", reason="log_body")


@asynccontextmanager
async def _open_runtime_bus() -> AsyncIterator[EventBusKafka]:
    bus = EventBusKafka(config=ModelKafkaEventBusConfig.default())
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


async def read_runtime_gateway(
    request: ModelGithubGatewayRequest, *, timeout_s: float
) -> ModelJobLogProbeReading:
    async with _open_runtime_bus() as bus:
        return await read_gateway(bus, request, timeout_s=timeout_s)


async def append_runtime_ledger(
    request: ModelWorkLedgerAppendRequest, *, timeout_s: float
) -> None:
    async with _open_runtime_bus() as bus:
        principal = build_confluent_auth_config(bus.config).get("sasl.username")
        if not principal:
            raise RuntimeError(
                "the runtime's ledger writer principal is not configured"
            )
        caller = WorkLedgerAppendCaller(bus, principal=principal)
        try:
            receipt = await caller.append(request, timeout_s=timeout_s)
        finally:
            await caller.stop()
    if receipt.request_id != request.request_id or receipt.status not in {
        EnumWorkLedgerAppendStatus.ACCEPTED,
        EnumWorkLedgerAppendStatus.DUPLICATE,
    }:
        raise RuntimeError(f"ledger append failed: {receipt.status.value}")
