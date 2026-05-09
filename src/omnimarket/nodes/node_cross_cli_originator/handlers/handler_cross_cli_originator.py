# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that builds and publishes a delegation envelope to the event bus.

This is an EFFECT handler — it performs external I/O (emit daemon socket call).

Topic source of truth: contract.yaml publish_topics. Never hardcode topic strings.
Bus resolution: EmitClient (Unix socket to emit daemon). No direct Kafka import.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_cross_cli_originator.models.model_cross_cli_originator_input import (
    ModelCrossCliOriginatorInput,
)
from omnimarket.nodes.node_cross_cli_originator.models.model_cross_cli_originator_result import (
    ModelCrossCliOriginatorResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_CMD_TOPIC_SUFFIX = "cross-cli-delegation-requested"


def _load_cmd_topic() -> str:
    """Load the delegation command topic from contract.yaml."""
    if not _CONTRACT_PATH.exists():
        msg = f"contract.yaml not found at {_CONTRACT_PATH}"
        raise RuntimeError(msg)

    with open(_CONTRACT_PATH) as fh:
        data = yaml.safe_load(fh) or {}

    publish_topics: list[str] = (data.get("event_bus") or {}).get(
        "publish_topics", []
    ) or []
    for topic in publish_topics:
        if _CMD_TOPIC_SUFFIX in topic:
            return topic

    msg = (
        f"contract.yaml at {_CONTRACT_PATH} does not declare a publish topic "
        f"containing {_CMD_TOPIC_SUFFIX!r}"
    )
    raise RuntimeError(msg)


_TOPIC_DELEGATION_CMD: str = _load_cmd_topic()


class _EmitClientProtocol(Protocol):
    """Structural protocol for the emit daemon client."""

    def emit_sync(self, event_type: str, payload: dict[str, Any]) -> str: ...

    def close(self) -> None: ...


class HandlerCrossCliOriginator:
    """Builds a delegation envelope and publishes it via the emit daemon.

    Uses EmitClient (Unix socket) for bus access — no direct Kafka import.
    Caller supplies an optional correlation_id; one is generated if absent.
    """

    def __init__(
        self,
        *,
        emit_client: _EmitClientProtocol | None = None,
    ) -> None:
        self._emit_client = emit_client

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    def handle(
        self, command: ModelCrossCliOriginatorInput
    ) -> ModelCrossCliOriginatorResult:
        """Build and publish the delegation envelope.

        Args:
            command: Delegation prompt and metadata from the CLI caller.

        Returns:
            ModelCrossCliOriginatorResult with event_id, correlation_id, topic.
        """
        correlation_id: UUID = command.correlation_id or uuid4()
        now = datetime.now(tz=UTC)

        envelope: dict[str, Any] = {
            "correlation_id": str(correlation_id),
            "session_id": command.session_id,
            "prompt": command.prompt,
            "task_type": command.task_type,
            "emitted_at": now.isoformat(),
            "source": "cross_cli_originator",
        }

        client = self._resolve_client()
        try:
            event_id = client.emit_sync(
                event_type="omnimarket.cross-cli-delegation-requested",
                payload=envelope,
            )
        finally:
            client.close()

        logger.info(
            "cross_cli_originator: published delegation envelope "
            "(correlation_id=%s, topic=%s, event_id=%s)",
            correlation_id,
            _TOPIC_DELEGATION_CMD,
            event_id,
        )

        return ModelCrossCliOriginatorResult(
            event_id=event_id,
            correlation_id=correlation_id,
            topic=_TOPIC_DELEGATION_CMD,
        )

    def _resolve_client(self) -> _EmitClientProtocol:
        if self._emit_client is not None:
            return self._emit_client
        from omnimarket.nodes.node_emit_daemon.client import EmitClient

        return EmitClient()


__all__: list[str] = ["HandlerCrossCliOriginator"]
