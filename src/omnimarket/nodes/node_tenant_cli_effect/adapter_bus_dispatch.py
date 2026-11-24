# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus caller of the ``onex tenant`` commands: omnibase_core's public ``publish_and_poll`` on the declared lane.

The broker is the one the target node's own overlay declares (``event_bus``), resolved the way every other bus caller
of a node resolves it; no flag or environment variable names a broker. The bus is the only thing this command line opens.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_core.cli.cli_run_node import publish_and_poll
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    TenantBusUnavailableError,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def default_wait_seconds() -> int:
    """The wait for an answer, from this node's contract (``descriptor.timeout_ms``)."""
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return max(1, int(raw["descriptor"]["timeout_ms"]) // 1000)


class BusTenantDispatch:
    """One command per call on the lane the target node's overlay declares."""

    def __init__(self, *, wait_seconds: int | None = None) -> None:
        self._wait_seconds = wait_seconds or default_wait_seconds()

    def lane(self, node_id: str) -> str | None:
        try:
            return resolve_dialable_broker(node_id).lane
        except NodeBrokerResolutionError:
            return None

    def dispatch(
        self,
        *,
        node_id: str,
        command_topic: str,
        response_topic: str,
        payload: dict[str, object],
        correlation_in_payload: bool,
    ) -> dict[str, object] | None:
        try:
            broker = resolve_dialable_broker(node_id)
            response = publish_and_poll(
                node_id=node_id,
                payload=payload,
                timeout=self._wait_seconds,
                bootstrap_servers=broker.bootstrap_servers or "",
                command_topic=command_topic,
                response_topic=response_topic,
                inject_payload_correlation_id=correlation_in_payload,
                client_security=broker.client_security(),
            )
        except (NodeBrokerResolutionError, LabRunBusError) as exc:
            raise TenantBusUnavailableError(str(exc)) from None
        except (ModelOnexError, OSError) as exc:
            raise TenantBusUnavailableError(
                f"the bus could not be used: {type(exc).__name__}"
            ) from None
        if response is None:
            return None
        body = response.get("payload", response)
        return body if isinstance(body, dict) else None


__all__: list[str] = ["BusTenantDispatch", "default_wait_seconds"]
