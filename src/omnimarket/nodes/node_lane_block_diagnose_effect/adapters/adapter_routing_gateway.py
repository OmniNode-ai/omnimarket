# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus adapter; topics, broker identity, timeouts and state roots are deployment facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from omnibase_core.cli.cli_run_node import publish_and_poll

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.models.model_lane_routing import (
    ModelFillBacklogCommand,
    ModelLaneBlockFacts,
    ModelOwnerDiagnosis,
    ModelOwnerMap,
    ModelTriageCommand,
)

from ..models.model_routing_bindings import (
    ModelRoutingAdmissionReply,
    ModelRoutingBindings,
    ModelRoutingBindReply,
    ModelRoutingEndpoint,
    ModelRoutingLaneReply,
    ModelRoutingTicketReply,
)


class HandlerLaneRoutingGateway:
    def __init__(
        self,
        bindings: ModelRoutingBindings,
        *,
        publish: Callable[..., dict[str, Any] | None] = publish_and_poll,
        broker: Callable[[str], ModelNodeBroker] = resolve_dialable_broker,
    ) -> None:
        self.bindings = bindings
        self.publish = publish
        self.broker = broker

    def _call(
        self, endpoint: ModelRoutingEndpoint, payload: dict[str, Any]
    ) -> dict[str, Any]:
        binding = self.broker(endpoint.node)
        response = self.publish(
            node_id=endpoint.node,
            payload=payload,
            timeout=self.bindings.timeout_seconds,
            bootstrap_servers=binding.bootstrap_servers or "",
            command_topic=endpoint.command_topic,
            response_topic=endpoint.terminal_topic,
            client_security=binding.client_security(),
        )
        if response is None:
            raise TimeoutError(
                f"{endpoint.node}: no terminal reply; pending claim retained"
            )
        result = response.get("payload", response)
        if not isinstance(result, dict):
            raise ValueError(f"{endpoint.node}: terminal payload is not an object")
        return result

    def diagnose(self, facts: ModelLaneBlockFacts) -> ModelOwnerDiagnosis:
        return ModelOwnerDiagnosis.model_validate(
            self._call(self.bindings.diagnosis, facts.model_dump(mode="json"))
        )

    def admit(
        self, command: ModelFillBacklogCommand, owners: ModelOwnerMap
    ) -> str | None:
        reply = ModelRoutingAdmissionReply.model_validate(
            self._call(
                self.bindings.admission,
                {
                    "command": command.model_dump(mode="json"),
                    "owner_map": owners.model_dump(mode="json"),
                },
            )
        )
        return None if reply.allowed else (reply.reason or "core admission refused")

    def fill(self, command: ModelFillBacklogCommand) -> str:
        return ModelRoutingLaneReply.model_validate(
            self._call(self.bindings.fill, command.model_dump(mode="json"))
        ).lane

    def bind(self, key: str, lane: str, items: tuple[str, ...]) -> None:
        reply = ModelRoutingBindReply.model_validate(
            self._call(
                self.bindings.bind,
                {
                    "key": key,
                    "lane": lane,
                    "item_ids": list(items),
                },
            )
        )
        if not set(items).issubset(reply.item_ids):
            raise ValueError("item binding terminal omitted an originating item")

    def exception(self, key: str, evidence: str, reason: str) -> None:
        result = self._call(
            self.bindings.exception,
            {
                "state_dir": self.bindings.exception_state_dir,
                "now": datetime.now(UTC).isoformat(),
                "idempotency_key": key,
                "row": {
                    "cls": "lane-owner-unresolved",
                    "key": key,
                    "source": "node_lane_block_diagnose_effect",
                    "evidence": evidence,
                    "tried": reason,
                    "proposed_fix": "Supply the original finding and named owner evidence",
                    "decision": False,
                },
            },
        )
        if result.get("status") not in ("appended", "repeat"):
            raise ValueError(
                "exception queue did not acknowledge the routing exception"
            )

    def ticket_repository(self, ticket: str) -> str | None:
        return ModelRoutingTicketReply.model_validate(
            self._call(self.bindings.ticket, {"ticket": ticket})
        ).repository

    def triage(self, request: ModelTriageCommand) -> str:
        return ModelRoutingLaneReply.model_validate(
            self._call(self.bindings.triage, request.model_dump(mode="json"))
        ).lane
