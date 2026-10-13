# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway that dispatches by the target node's own contract, in this process (OMN-20677).

The ledger and git evidence are host-bound resources, so the compute and effect
nodes run beside the orchestrator on the operator's host. Each hop still goes the
way the bus carries it: the request is validated into the operation's input model
from JSON, the handler the contract binds to that operation answers, and the
answer is dumped back to JSON. A field two nodes name differently fails here.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, JsonValue


class ContractGatewayError(RuntimeError):
    """The target node's contract does not bind the operation."""


def _load(path: str) -> Any:
    module, _, name = path.rpartition(".")
    return getattr(importlib.import_module(module), name)


class ContractGateway:
    """Resolves ``(node, operation)`` through ``contract.yaml``; ``handlers`` overrides a binding."""

    def __init__(self, handlers: dict[tuple[str, str], Any] | None = None) -> None:
        self._handlers: dict[tuple[str, str], Any] = dict(handlers or {})

    def _binding(self, node: str, operation: str) -> tuple[Any, type[BaseModel]]:
        contract = yaml.safe_load(
            files(f"omnimarket.nodes.{node}")
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
        for entry in contract["handler_routing"]["handlers"]:
            if entry["operation"] != operation:
                continue
            key = (node, operation)
            if key not in self._handlers:
                handler = entry["handler"]
                self._handlers[key] = getattr(
                    importlib.import_module(handler["module"]), handler["name"]
                )()
            return self._handlers[key], _load(str(entry["input_model"]))
        raise ContractGatewayError(f"{node} binds no operation {operation}")

    async def dispatch(
        self, node: str, operation: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        handler, input_model = self._binding(node, operation)
        answer = handler.handle(input_model.model_validate(payload))
        if inspect.isawaitable(answer):
            answer = await answer
        if not isinstance(answer, BaseModel):
            raise ContractGatewayError(
                f"{node}.{operation} answered {type(answer).__name__}"
            )
        dumped: dict[str, JsonValue] = answer.model_dump(mode="json")
        return dumped
