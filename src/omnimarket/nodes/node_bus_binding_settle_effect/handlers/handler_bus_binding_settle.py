# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle the open bus-binding exception rows of one lane through the exception actor (OMN-21959).

The bus binding calls this after a healthy durable read. The rows it gives the actor are the open, non-decision
rows of a class the contract names (``settle_policy.classes``), written by the bus binding and keyed to the bound
lane. The actor resolves a row whose cause is gone and leaves one open row for a case it cannot settle; the
ports it uses (the durable health read and the local outbox) are injected by the caller.
"""

from __future__ import annotations

from importlib import resources

import yaml

from omnimarket.handlers.bus_binding.exception_actor import ActorPorts, settle_case
from omnimarket.nodes.node_bus_binding_settle_effect.models.model_bus_binding_settle import (
    ModelBusBindingSettleRequest,
    ModelBusBindingSettleResult,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_queue,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionQueueRequest,
)


def settle_policy() -> tuple[str, frozenset[str]]:
    """(source, classes) the contract declares for this step."""
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_bus_binding_settle_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    policy = contract["settle_policy"]
    classes = frozenset(str(cls) for cls in policy["classes"])
    if not classes:
        raise ValueError("bus binding settle_policy.classes must name a class")
    return str(policy["source"]), classes


class HandlerBusBindingSettle:
    """Give each open row of the bound lane to the actor; return what the actor did with it."""

    def __init__(
        self,
        ports: ActorPorts,
        *,
        source: str | None = None,
        classes: frozenset[str] | None = None,
    ) -> None:
        declared_source, declared_classes = settle_policy()
        self._ports = ports
        self._source = source if source is not None else declared_source
        self._classes = classes if classes is not None else declared_classes

    def handle(
        self, request: ModelBusBindingSettleRequest
    ) -> ModelBusBindingSettleResult:
        opened = (
            create_handler_exception_queue()
            .handle(
                ModelExceptionQueueRequest(
                    request.state_dir / "exception-queue", "orchestrator"
                )
            )
            .rows
        )
        key = f"lane={request.lane}"
        resolutions = tuple(
            settle_case(request.state_dir, row, self._ports, now=request.observed_at)
            for row in opened
            if row.get("cls") in self._classes
            and row.get("source") == self._source
            and row.get("key") == key
            and not row.get("decision")
        )
        return ModelBusBindingSettleResult(lane=request.lane, resolutions=resolutions)
