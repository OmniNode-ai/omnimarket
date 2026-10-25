# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Collect contract expectations and broker membership for one declared lane."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.runtime_coverage import (
    ModelCommandSubscription,
    ModelRuntimeCoverageRequest,
    ModelRuntimeCoverageSnapshot,
    ModelRuntimeMember,
    ModelRuntimeRegistryEndpoint,
)
from omnimarket.nodes.node_runtime_coverage_effect.handlers.broker_reader import (
    read_members,
)
from omnimarket.nodes.node_runtime_coverage_effect.handlers.registry_reader import (
    read_registry,
)

MemberReader = Callable[
    [Mapping[str, str], Mapping[str, str]],
    tuple[tuple[ModelRuntimeMember, ...], tuple[str, ...]],
]


def collect_commands(
    roots: tuple[str, ...],
    runtime_lanes: tuple[str, ...],
    environment: str,
) -> tuple[tuple[ModelCommandSubscription, ...], dict[str, str]]:
    """Read only the selected roots, never a workspace-wide recursive crawl."""
    from omnibase_infra.utils.util_consumer_group import normalize_kafka_identifier

    commands: dict[tuple[str, str], ModelCommandSubscription] = {}
    prefixes: dict[str, str] = {}
    for root in roots:
        directory = Path(root)
        if not directory.is_dir():
            raise ValueError(f"contract root is unreadable: {directory}")
        paths = sorted(directory.glob("*/contract.yaml"))
        if not paths:
            raise ValueError(f"contract root has no contracts: {directory}")
        # Package identity is the owner immediately above nodes/.
        package = directory.parent.name
        for path in paths:
            raw = yaml.safe_load(path.read_text())
            if not isinstance(raw, dict):
                raise ValueError(f"contract is not a mapping: {path}")
            lanes = raw.get("runtime_lanes") or raw.get("descriptor", {}).get(
                "runtime_lanes"
            )
            if lanes and not set(lanes).intersection(runtime_lanes):
                continue
            name = raw["name"]
            for topic in (raw.get("event_bus") or {}).get("subscribe_topics", ()):
                if isinstance(topic, str) and topic.startswith("onex.cmd."):
                    key = (name, topic)
                    commands[key] = ModelCommandSubscription(node=name, topic=topic)
                    identity = (environment, package, name, "consume")
                    prefix = (
                        ".".join(normalize_kafka_identifier(v) for v in identity) + "."
                    )
                    prefixes[prefix] = name
    return tuple(commands[key] for key in sorted(commands)), prefixes


def resolve_broker(lane: str) -> tuple[str, str, dict[str, str]]:
    """Use the same declared lane and machine credential as onex scm."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding

    selection, transport = resolve_lane_binding(lane)
    declaration = yaml.safe_load(selection.declared_in.read_text())
    environment = declaration["lanes"][lane]["broker_topology"]["runtime_environment"]
    if not isinstance(environment, str) or not environment:
        raise ValueError("lane declares no runtime environment")
    config = {
        "bootstrap.servers": selection.bootstrap_servers,
        "security.protocol": selection.security_protocol,
    }
    if transport is not None and transport.is_sasl:
        if transport.sasl_password is None or transport.sasl_username is None:
            raise ValueError("lane credential incomplete")
        config.update(
            {
                "sasl.mechanism": transport.sasl_mechanism or "",
                "sasl.username": transport.sasl_username,
                "sasl.password": transport.sasl_password.get_secret_value(),
            }
        )
    return selection.bootstrap_servers, environment, config


class HandlerRuntimeCoverageEffect:
    def __init__(
        self,
        member_reader: MemberReader = read_members,
        broker_resolver: Callable[
            [str], tuple[str, str, dict[str, str]]
        ] = resolve_broker,
        registry_reader: Callable[
            [ModelRuntimeRegistryEndpoint], Awaitable[frozenset[tuple[str, str]]]
        ] = read_registry,
    ) -> None:
        self._registry = registry_reader
        self._read = member_reader
        self._resolve = broker_resolver

    async def handle(
        self, request: ModelRuntimeCoverageRequest
    ) -> ModelRuntimeCoverageSnapshot:
        broker = ""
        commands: tuple[ModelCommandSubscription, ...] = ()
        try:
            broker, environment, config = self._resolve(request.bus_lane)
            commands, prefixes = collect_commands(
                request.contract_roots, request.runtime_lanes, environment
            )
            if any(e.bus_lane != request.bus_lane for e in request.runtime_registries):
                raise ValueError("runtime registry belongs to a different bus lane")
            members, unreadable = await asyncio.to_thread(self._read, config, prefixes)
            registry_errors: list[str] = []
            for endpoint in request.runtime_registries:
                try:
                    declared = await self._registry(endpoint)
                except Exception as exc:
                    registry_errors.append(
                        f"{endpoint.host}/{endpoint.runtime_id}: registry unreadable ({type(exc).__name__})"
                    )
                    continue
                # A manifest only corroborates live membership. It never
                # adds a server when the broker has no attached member.
                members = tuple(
                    m.model_copy(
                        update={
                            "host": endpoint.host,
                            "topics": tuple(
                                topic
                                for topic in m.topics
                                if (m.node, topic) in declared
                            ),
                        }
                    )
                    if m.runtime_id == endpoint.runtime_id
                    else m
                    for m in members
                    if m.runtime_id != endpoint.runtime_id
                    or any((m.node, topic) in declared for topic in m.topics)
                )
            return ModelRuntimeCoverageSnapshot(
                bus_lane=request.bus_lane,
                broker=broker,
                observed_at=datetime.now(UTC),
                commands=commands,
                members=members,
                unreadable_nodes=unreadable,
                registry_errors=tuple(registry_errors),
            )
        except Exception as exc:
            # Error text from a client may contain connection credentials.
            # Keep the failure class only, and never turn it into zero servers.
            return ModelRuntimeCoverageSnapshot(
                bus_lane=request.bus_lane,
                broker=broker,
                observed_at=datetime.now(UTC),
                commands=commands,
                error=f"coverage evidence unreadable: {type(exc).__name__}",
            )
