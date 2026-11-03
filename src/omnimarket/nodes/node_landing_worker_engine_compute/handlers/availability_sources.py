# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the worker-engine choice reads each engine's availability (OMN-21958).

``FactsAvailabilitySource`` reads the typed facts of the request and is the node's input. ``PlacementAvailabilitySource``
is the deprecated adapter for a caller that still passes the placement module and the env switches: it asks
the module lazily, call for call as the node did before, so a route not taken probes nothing. The routing
itself lives in the handler and sees only the ``AvailabilitySource`` shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_landing_worker_engine_compute.models.model_landing_worker_engine import (
    ModelLandingWorkerEngineRequest,
)

CODEX_MECHANICAL_ENV = "LANDING_CODEX_MECHANICAL"
CODEX_FALLBACK_ENV = "LANDING_CODEX_FALLBACK"


class AvailabilitySource(Protocol):
    """The reads the routing makes; each one is a probe in the legacy adapter."""

    codex_first_allowed: bool
    codex_fallback_allowed: bool

    @property
    def codex_engine(self) -> str: ...

    def resolve(self, requested: str) -> str: ...

    def codex_pick(self) -> Any: ...

    def codex_skips(self) -> Mapping[str, str]: ...

    def claude_pick(self, requested: str) -> Any: ...

    def claude_skips(self, requested: str) -> Mapping[str, str]: ...


class FactsAvailabilitySource:
    """Availability as supplied by the caller; an engine it does not carry refuses, never reads as full."""

    def __init__(self, request: ModelLandingWorkerEngineRequest) -> None:
        if request.availability is None or request.config is None:
            raise ValueError("typed engine facts need availability and config")
        self._by_engine = {a.engine: a for a in request.availability}
        if len(self._by_engine) != len(request.availability):
            raise ValueError("availability names an engine twice")
        self._codex = request.config.codex_engine
        self.codex_first_allowed = request.config.codex_mechanical
        self.codex_fallback_allowed = (
            request.config.codex_mechanical and request.config.codex_fallback
        )

    @property
    def codex_engine(self) -> str:
        return self._codex

    def _of(self, engine: str) -> Any:
        found = self._by_engine.get(engine)
        if found is None:
            raise ValueError(f"availability carries no engine {engine}")
        return found

    def resolve(self, requested: str) -> str:
        return requested

    def codex_pick(self) -> Any:
        return self._of(self._codex).pick

    def codex_skips(self) -> Mapping[str, str]:
        return dict(self._of(self._codex).skips)

    def claude_pick(self, requested: str) -> Any:
        return self._of(requested).pick

    def claude_skips(self, requested: str) -> Mapping[str, str]:
        return dict(self._of(requested).skips)


class PlacementAvailabilitySource:
    """Deprecated: the placement module and the env switches of a caller not yet moved to typed facts."""

    def __init__(self, request: ModelLandingWorkerEngineRequest) -> None:
        self._lp = request.placement
        self._readings = list(request.readings)
        self._allowed = request.codex_allowed
        self.codex_first_allowed = True
        self.codex_fallback_allowed = all(
            request.env.get(name, "1").strip() != "0"
            for name in (CODEX_MECHANICAL_ENV, CODEX_FALLBACK_ENV)
        )

    @property
    def codex_engine(self) -> str:
        return str(self._lp.CODEX_ENGINE)

    def resolve(self, requested: str) -> str:
        return str(self._lp.resolve_engine(requested))

    def codex_pick(self) -> Any:
        return self._lp.choose_codex(self._readings, self._allowed)

    def codex_skips(self) -> Mapping[str, str]:
        result: Mapping[str, str] = self._lp.preflight_skips(
            self._readings, self._lp.CODEX_ENGINE, self._allowed
        )
        return result

    def claude_pick(self, requested: str) -> Any:
        return self._lp.choose(requested, self._readings)

    def claude_skips(self, requested: str) -> Mapping[str, str]:
        result: Mapping[str, str] = self._lp.preflight_skips(
            self._readings, self._lp.resolve_engine(requested)
        )
        return result


def engine_source_of(request: ModelLandingWorkerEngineRequest) -> AvailabilitySource:
    if request.availability is not None:
        return FactsAvailabilitySource(request)
    return PlacementAvailabilitySource(request)
