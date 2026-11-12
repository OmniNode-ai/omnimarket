# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production bus request of scenario R8's tenant reads (OMN-23822).

The lane publishes a tenant read command and waits for the node's terminal over the
bus the contract names. The topics come from the tenant node's contract, so no topic
is hard-coded here; the bus and lane come from the overlay, and the workspace root from
the environment variable the contract names.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from omnibase_infra.errors import InfraConnectionError, InfraTimeoutError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
)
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    BusRequest,
    ModelTenantReadAnswer,
)


def _load_topics(package: str) -> ModelScmTopics:
    """The tenant node's dispatch topics, read from its contract like the ticket CLI reads its own."""
    contract = yaml.safe_load(
        resources.files(package).joinpath("contract.yaml").read_text()
    )
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=dispatch["command_topic"], **dispatch["terminal_events"]
    )


def failed_terminal_reason(payload: Mapping[str, Any]) -> str:
    """The cause a failed tenant read terminal carries, whichever shape answered.

    The tenant node answers with ``reason`` and ``detail``; the runtime's boundary failure terminal (a command that
    reached no dispatcher, a handler that raised) answers with ``failure_code`` and ``failure_reason``. A failed
    terminal that carries neither names the keys it did carry, so a cause is never reported as ``None``.
    """
    code = payload.get("reason") or payload.get("failure_code")
    text = payload.get("detail") or payload.get("failure_reason")
    if code and text:
        return f"{code}: {text}"
    if code or text:
        return str(code or text)
    keys = ", ".join(sorted(payload)) or "none"
    return f"failed terminal carries no reason or failure code (payload keys: {keys})"


def build_bus_request(
    overlay: ModelScenarioTenantReadOverlay,
    *,
    savings_node: str,
    delegations_node: str,
    workspace_env: str,
    environment: Mapping[str, str],
) -> BusRequest:
    """The callable the lane injects: it opens the bus and publishes one tenant read command."""
    topics = {
        EnumTenantReadKind.SAVINGS: _load_topics(savings_node),
        EnumTenantReadKind.DELEGATIONS: _load_topics(delegations_node),
    }

    def request(
        command: ModelTenantReadCommand, timeout_s: float
    ) -> ModelTenantReadAnswer:
        omnibase_path = environment.get(workspace_env, "").strip()
        bus_opts: dict[str, Any] = {
            "bus": overlay.bus,
            "bus_lane": overlay.bus_lane,
            "kafka_bootstrap": None,
            "omnibase_path": Path(omnibase_path) if omnibase_path else None,
        }

        async def call() -> ModelScmTerminal:
            async with _open_bus(bus_opts) as opened:
                return await ScmBusCaller(opened).call_topics(
                    topics=topics[command.kind],
                    request=command,
                    correlation_id=command.correlation_id,
                    timeout_s=timeout_s,
                )

        try:
            terminal = asyncio.run(call())
        except (
            TimeoutError,
            LabRunBusError,
            InfraTimeoutError,
            InfraConnectionError,
        ) as exc:
            return ModelTenantReadAnswer(
                completed=False,
                reason=f"no tenant read terminal: {exc or type(exc).__name__}",
            )
        payload = terminal.payload
        if terminal.failed:
            return ModelTenantReadAnswer(
                completed=False, reason=failed_terminal_reason(payload)
            )
        body = payload.get("body")
        if not isinstance(body, dict):
            return ModelTenantReadAnswer(
                completed=False, reason="the completed terminal carries no body"
            )
        return ModelTenantReadAnswer(completed=True, body=dict(body))

    return request
