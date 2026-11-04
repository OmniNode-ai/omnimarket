# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker and client security a node's bus caller dials, from the node's contract overlay (OMN-22362).

Operator ruling 2026-11-03: "The default broker should be in the contract overlay." ``onex run-node``
(omnibase_core) asks the resolver registered under the ``onex.node_broker_resolvers`` entry-point
group, and its private ``_resolve_client_security_config`` (which read ``KAFKA_SECURITY_PROTOCOL`` and
``KAFKA_SASL_*``) is gone. The callers in this package that dial a node over the bus go the same way:

* Exactly one registered resolver answers when one is installed. It is consumed structurally, with
  no import of the core binding class, so the answer is read the same from a core that has the class
  and the same code runs on a core that does not.
* Where no resolver is registered (the omnibase_infra this package pins predates it), the same
  ``event_bus`` declaration is read here: the node's overlay under the workspace config root, then
  the lane it names resolved to its broker and client transport with the readers ``onex delegate``
  and ``onex scm`` use. This is the same declaration read the same way, never an environment
  variable or a default broker. The branch leaves with the pin move that brings the resolver.

``KAFKA_BOOTSTRAP_SERVERS`` and ``KAFKA_SASL_*`` are not consulted for the broker or its security
protocol. The SASL identity a lab host or CI runner holds only supplies the credential of a lane the
declaration already selected, as it does for every other lane client.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Final, Protocol

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    NODE_OVERLAYS_RELATIVE_PATH,
    resolve_node_event_bus,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker

NODE_BROKER_RESOLVER_ENTRY_POINT_GROUP: Final[str] = "onex.node_broker_resolvers"
_SASL_ENVIRONMENT: Final[tuple[str, str]] = (
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
)


class _RegisteredResolver(Protocol):
    """The shape core's ``ProtocolNodeBrokerResolver`` has, read without importing the binding it answers."""

    def resolve_node_broker(self, node_id: str) -> Any: ...


class NodeBrokerResolutionError(LabRunBusError):
    """The broker a node is reached on is not declared, or could not be resolved from its overlay."""


def _workspace_root(environ: Mapping[str, str]) -> Path | None:
    """``$OMNIBASE_PATH``, else ``$OMNI_HOME``: where the workspace is, never which broker."""
    for name in ("OMNIBASE_PATH", "OMNI_HOME"):
        value = environ.get(name, "").strip()
        if value:
            return Path(value)
    return None


def _registered_resolver() -> _RegisteredResolver | None:
    """The one registered node broker resolver, or None when none is installed."""
    providers = list(entry_points(group=NODE_BROKER_RESOLVER_ENTRY_POINT_GROUP))
    if not providers:
        return None
    if len(providers) > 1:
        found = ", ".join(sorted(f"{ep.name} ({ep.value})" for ep in providers))
        raise NodeBrokerResolutionError(
            f"more than one node broker resolver is registered ({found}); "
            "uninstall the extra provider so one declaration answers."
        )
    loaded = providers[0].load()
    resolver: _RegisteredResolver = loaded() if isinstance(loaded, type) else loaded
    if not callable(getattr(resolver, "resolve_node_broker", None)):
        raise NodeBrokerResolutionError(
            f"node broker resolver {providers[0].value!r} does not provide resolve_node_broker"
        )
    return resolver


def _from_registered(resolver: _RegisteredResolver, node: str) -> ModelNodeBroker:
    """Read a resolver's binding structurally: bus kind, address, lane, overlay path, security."""
    from omnibase_core.errors.model_onex_error import ModelOnexError
    from pydantic import SecretStr

    try:
        binding = resolver.resolve_node_broker(node)
    except ModelOnexError as exc:
        overlay = getattr(exc, "overlay_path", None)
        suffix = f" (overlay {overlay})" if overlay else ""
        raise NodeBrokerResolutionError(f"{exc.message}{suffix}") from None
    if not getattr(binding.bus_type, "requires_broker", False):
        return ModelNodeBroker(kind="inmemory", overlay_path=binding.overlay_path)
    security = binding.client_security()
    password = security.get("sasl.password")
    return ModelNodeBroker(
        kind="kafka",
        overlay_path=binding.overlay_path,
        lane=binding.lane,
        bootstrap_servers=binding.bootstrap_servers,
        security_protocol=security.get("security.protocol"),
        sasl_mechanism=security.get("sasl.mechanism"),
        sasl_username=security.get("sasl.username"),
        sasl_password=SecretStr(password) if password is not None else None,
    )


def _from_overlay(
    node: str, workspace_root: Path | None, environ: Mapping[str, str]
) -> ModelNodeBroker:
    from omnibase_infra.cli.delegate_lane import DelegateLaneSelectionError
    from omnibase_infra.cli.delegate_lane_credentials import (
        DelegateLaneCredentialError,
        resolve_lane_binding,
    )
    from pydantic import SecretStr

    declared, path = resolve_node_event_bus(node, workspace_root)
    if declared is None:
        raise NodeBrokerResolutionError(
            f"{node} declares no event_bus in its contract overlay "
            f"({path or Path('<workspace config root>') / NODE_OVERLAYS_RELATIVE_PATH / node / 'overlay.yaml'}) "
            "and names no broker. Declare `event_bus: {type: kafka, lane: <lane id>}` there "
            "(the broker address stays in the lane declaration). The broker is never taken from "
            "the environment or a flag."
        )
    if declared.type == "inmemory":
        return ModelNodeBroker(kind="inmemory", overlay_path=path)
    lane = str(declared.lane)
    try:
        selection, transport = resolve_lane_binding(
            lane,
            environ={
                "OMNIBASE_PATH": str(workspace_root or ""),
                **{name: environ.get(name, "") for name in _SASL_ENVIRONMENT},
            },
        )
    except (DelegateLaneSelectionError, DelegateLaneCredentialError) as exc:
        raise NodeBrokerResolutionError(
            f"the lane {lane!r} named by {path} is not usable: {exc}"
        ) from exc
    if transport is not None:
        return ModelNodeBroker(
            kind="kafka",
            overlay_path=path,
            lane=lane,
            bootstrap_servers=transport.bootstrap_servers,
            security_protocol=transport.security_protocol,
            sasl_mechanism=transport.sasl_mechanism,
            sasl_username=transport.sasl_username,
            sasl_password=transport.sasl_password,
        )
    # A lab host or CI runner holds the SASL identity in its environment, not a client store: the
    # credential resolver stepped aside and that environment answers for the declared lane.
    username, password = (environ.get(name, "").strip() for name in _SASL_ENVIRONMENT)
    return ModelNodeBroker(
        kind="kafka",
        overlay_path=path,
        lane=lane,
        bootstrap_servers=selection.bootstrap_servers,
        security_protocol=selection.security_protocol,
        sasl_mechanism=selection.sasl_mechanism,
        sasl_username=username or None,
        sasl_password=SecretStr(password) if password else None,
    )


def resolve_node_broker(
    node: str, *, environ: Mapping[str, str] | None = None
) -> ModelNodeBroker:
    """The broker ``node`` is reached on, from its contract overlay and from nothing else.

    Raises:
        NodeBrokerResolutionError: no ``event_bus`` is declared, the declaration is malformed, the
            lane is not usable, or two resolvers are registered. The message names the overlay path.
    """
    source = os.environ if environ is None else environ
    resolver = _registered_resolver()
    if resolver is not None:
        return _from_registered(resolver, node)
    return _from_overlay(node, _workspace_root(source), source)


def resolve_dialable_broker(
    node: str, *, environ: Mapping[str, str] | None = None
) -> ModelNodeBroker:
    """The broker a caller that publishes to ``node`` over the bus dials; ``type: inmemory`` is refused.

    These callers send a command to a node that runs in the lane's runtime, where its credentials
    and tools are, so an overlay that hosts the node in this process names no broker for them.
    """
    broker = resolve_node_broker(node, environ=environ)
    if broker.kind != "kafka":
        raise NodeBrokerResolutionError(
            f"{node} declares event_bus type inmemory in {broker.overlay_path}, so it names no "
            "broker. This caller publishes to the node over the bus and does not host it in this "
            "process; declare `event_bus: {type: kafka, lane: <lane id>}` there."
        )
    return broker


__all__ = [
    "NODE_BROKER_RESOLVER_ENTRY_POINT_GROUP",
    "NodeBrokerResolutionError",
    "resolve_dialable_broker",
    "resolve_node_broker",
]
