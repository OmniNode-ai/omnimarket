# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The corpus bus request of scenario R9 (OMN-24554).

One committed bad request is published as a raw bus command on the delegation runtime's command topic, exactly as it
is committed: no field is added, dropped or coerced. The topics come from the runtime node's contract (the R8
adapter's ``load_node_topics``), so no topic is hard-coded here; the bus and lane come from the same overlay document
R8's tenant reads use, and the workspace root from the environment variable the node contract names.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnibase_infra.errors import InfraConnectionError, InfraTimeoutError
from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_bus_request import (
    failed_terminal_reason,
    load_node_topics,
)


class ModelCorpusPayload(BaseModel):
    """One corpus case's payload exactly as it is published: no field is named, none may be dropped."""

    model_config = ConfigDict(extra="allow", frozen=True)


class ModelCorpusAnswer(BaseModel):
    """The terminal one corpus payload answered with: refused, succeeded, or no terminal at all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answered: bool
    failed: bool
    payload: Mapping[str, JsonValue]
    reason: str | None


CorpusRequest = Callable[[Mapping[str, JsonValue], float], ModelCorpusAnswer]


def build_corpus_request(
    overlay: ModelScenarioTenantReadOverlay,
    *,
    command_node: str,
    workspace_env: str,
    environment: Mapping[str, str],
) -> CorpusRequest:
    """The callable the scenario injects: it opens the bus and publishes one raw corpus payload."""
    topics = load_node_topics(command_node)

    def request(
        payload: Mapping[str, JsonValue], timeout_s: float
    ) -> ModelCorpusAnswer:
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
                    topics=topics,
                    request=ModelCorpusPayload(**payload),
                    correlation_id=uuid4(),
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
            return ModelCorpusAnswer(
                answered=False,
                failed=False,
                payload={},
                reason=f"no terminal: {exc or type(exc).__name__}",
            )
        return ModelCorpusAnswer(
            answered=True,
            failed=terminal.failed,
            payload=dict(terminal.payload),
            reason=failed_terminal_reason(terminal.payload)
            if terminal.failed
            else None,
        )

    return request
