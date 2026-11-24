# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab's local chat servers that answer now, least busy first (scenario R2).

Scenario R2 adds two local servers to a fresh customer home. The product reaches a local model through its local tier:
the lab servers the routing declarations place in a spread group, the least busy first. This reads that same set, asks
each server which model it lists (the add test delegation pins to a listed id) and returns the ones that answered. No
address is named here: the servers come from the routing declarations, so a host that stops serving drops out and a host
that starts serving comes in with no settings change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit

from omnimarket.handlers.routing.byok_model_discovery import get_models_json
from omnimarket.handlers.routing.endpoint_load_reader import read_endpoint_load
from omnimarket.handlers.routing.spread_load_snapshot import spread_load_targets
from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)
from omnimarket.models.model_delegation_scenario import ModelScenarioR2RunCommand
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    ModelLocalServer,
    ModelLocalServerPool,
)

#: A server that does not list its models inside this budget does not answer.
LIST_TIMEOUT_SECONDS = 4.0

Targets = Callable[[], Mapping[str, str]]
LoadReader = Callable[[Mapping[str, str]], ModelEndpointLoadSnapshot]
ModelLister = Callable[[str], Sequence[str] | None]


def _origin(endpoint_url: str) -> str:
    parts = urlsplit(endpoint_url)
    return f"{parts.scheme}://{parts.netloc}"


def _read_load(targets: Mapping[str, str]) -> ModelEndpointLoadSnapshot:
    return read_endpoint_load(targets, as_of=datetime.now(UTC))


def list_models(base_url: str) -> tuple[str, ...] | None:
    """The model ids the server at ``base_url`` lists, or ``None`` when it does not answer a model list."""
    try:
        listed = get_models_json(
            url=f"{base_url}/v1/models", timeout_seconds=LIST_TIMEOUT_SECONDS
        )
    except Exception:  # any failure is the answer: the server does not answer
        return None
    data = listed.get("data") if isinstance(listed, dict) else None
    if not isinstance(data, list):
        return None
    return tuple(
        str(row["id"]) for row in data if isinstance(row, dict) and row.get("id")
    )


class HandlerLabLocalPool:
    """The live local servers of the lab's spread groups, least waiting requests first."""

    def __init__(
        self,
        *,
        targets: Targets = spread_load_targets,
        load: LoadReader = _read_load,
        lister: ModelLister = list_models,
    ) -> None:
        self._targets = targets
        self._load = load
        self._lister = lister

    def handle(self, request: ModelScenarioR2RunCommand) -> ModelLocalServerPool:
        return ModelLocalServerPool(servers=self.live_servers())

    def live_servers(self) -> tuple[ModelLocalServer, ...]:
        targets = self._targets()
        if not targets:
            return ()
        snapshot = self._load(targets)
        # Least waiting first; a server whose queue could not be read goes after every one that could, in declared order.
        keys: dict[str, tuple[bool, int, int]] = {}
        for index, ref in enumerate(targets):
            reading = snapshot.load_of(ref)
            waiting = None if reading is None else reading.waiting
            keys[ref] = (waiting is None, waiting or 0, index)
        ordered = sorted(targets, key=lambda ref: keys[ref])
        origins: list[str] = []
        for ref in ordered:
            origin = _origin(targets[ref])
            if origin not in origins:
                origins.append(origin)
        with ThreadPoolExecutor(max_workers=len(origins)) as pool:
            listings = list(pool.map(self._lister, origins))
        return tuple(
            ModelLocalServer(base_url=origin, model_id=models[0])
            for origin, models in zip(origins, listings, strict=True)
            if models
        )


__all__ = ["HandlerLabLocalPool", "list_models"]
