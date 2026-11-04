# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census/alarm alias parity check takes both registries as deployment data."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lane_census_alias_parity_compute.models import (
    ModelLaneCensusAliasParityRequest,
    ModelLaneCensusAliasParityResult,
)
from omnimarket.nodes.node_lane_census_alias_parity_compute.node import (
    NodeLaneCensusAliasParityCompute,
)

pytestmark = pytest.mark.unit


def check(
    hosts: dict[str, tuple[str, ...]], aliases: dict[str, str]
) -> ModelLaneCensusAliasParityResult:
    return NodeLaneCensusAliasParityCompute().handle(
        ModelLaneCensusAliasParityRequest(manifest_hosts=hosts, alarm_aliases=aliases)
    )


def test_recorded_hp_gen8_addition_requires_only_matching_data() -> None:
    # The recorded host-undeclared failure was hp-gen8 (collector exit 5).
    # Adding its manifest row formerly required extending a frozen Python constant.
    hosts = {"lab-001": ("host-a",)}
    aliases = {"h001": "host-a"}
    assert check(hosts, aliases).matches
    hosts["lab-051"] = ("hp-gen8",)
    refused = check(hosts, aliases)
    assert not refused.matches
    assert refused.missing_from_alarm == ("h051",)
    assert refused.missing_from_census == ()
    assert refused.different_aliases == ()
    aliases["h051"] = "hp-gen8"
    accepted = check(hosts, aliases)
    assert accepted.matches
    assert accepted.missing_from_alarm == accepted.missing_from_census == ()
    assert accepted.different_aliases == ()


def test_parity_preserves_exact_alias_equality() -> None:
    for aliases in (
        ("other",),
        ("HOST-A",),
        ("host-a.local",),
        ("host-a", "extra"),
        (),
    ):
        result = check({"lab-001": aliases}, {"h001": "host-a"})
        assert not result.matches
        assert result.different_aliases == ("h001",)
    assert check({"lab-001": ("host-a",)}, {"h001": "host-a"}).matches


def test_parity_refuses_an_alarm_host_absent_from_the_census() -> None:
    result = check({"lab-001": ("host-a",)}, {"h001": "host-a", "h002": "host-b"})
    assert not result.matches
    assert result.missing_from_census == ("h002",)


def test_result_is_deterministic_and_does_not_mutate_the_request() -> None:
    request = ModelLaneCensusAliasParityRequest(
        manifest_hosts={"lab-003": ("c",), "lab-002": ("b",), "lab-001": ("a",)},
        alarm_aliases={"h004": "d", "h001": "wrong"},
    )
    before = request.model_dump()
    first = NodeLaneCensusAliasParityCompute().handle(request)
    assert first == NodeLaneCensusAliasParityCompute().handle(request)
    assert first.missing_from_alarm == ("h002", "h003")
    assert first.missing_from_census == ("h004",)
    assert first.different_aliases == ("h001",)
    assert request.model_dump() == before


def test_input_is_explicit_and_refuses_empty_registries_and_extra_fields() -> None:
    valid = {"manifest_hosts": {"lab-001": ["a"]}, "alarm_aliases": {"h001": "a"}}
    for key in valid:
        broken = dict(valid)
        del broken[key]
        with pytest.raises(ValidationError):
            ModelLaneCensusAliasParityRequest.model_validate(broken)
        with pytest.raises(ValidationError):
            ModelLaneCensusAliasParityRequest.model_validate({**valid, key: {}})
    with pytest.raises(ValidationError):
        ModelLaneCensusAliasParityRequest.model_validate(
            {**valid, "ignore_extra_hosts": True}
        )


def test_host_id_conversion_cannot_silently_overwrite_a_host() -> None:
    with pytest.raises(ValidationError, match="same alarm host"):
        ModelLaneCensusAliasParityRequest(
            manifest_hosts={"lab-001": ("a",), "001": ("b",)},
            alarm_aliases={"h001": "a"},
        )


async def test_runtime_command_checks_the_recorded_host_through_declared_topics() -> (
    None
):
    """Installed discovery and auto-wiring dispatch both refusal and data-only repair."""
    import asyncio
    import json
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_lane_census_alias_parity_compute.node import CONTRACT

    node_name = "node_lane_census_alias_parity_compute"
    contracts = tuple(c for c in discover_contracts().contracts if c.name == node_name)
    assert len(contracts) == 1
    raw = yaml.safe_load(CONTRACT.read_text())
    command_topic = raw["runtime_dispatch"]["command_topic"]
    completed_topic = raw["terminal_event"]
    assert raw["event_bus"]["subscribe_topics"] == [command_topic]
    assert raw["event_bus"]["publish_topics"] == [completed_topic]
    completed: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="census-alias-parity")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await completed.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed_topic, group_id="parity-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            node_name: "wired"
        }
        assert report.results[0].topics_subscribed == (command_topic,)
        engine.freeze()
        for aliases, expected in [
            ({"h001": "host-a"}, False),
            ({"h001": "host-a", "h051": "hp-gen8"}, True),
        ]:
            correlation_id = uuid4()
            request = ModelLaneCensusAliasParityRequest(
                manifest_hosts={"lab-001": ("host-a",), "lab-051": ("hp-gen8",)},
                alarm_aliases=aliases,
            )
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation_id,
                event_type=command_topic,
            )
            await bus.publish(
                command_topic, None, envelope.model_dump_json().encode(), None
            )
            event = await asyncio.wait_for(completed.get(), timeout=3)
            assert event["correlation_id"] == str(correlation_id)
            result = event["payload"]
            assert result["matches"] is expected
            assert result["missing_from_alarm"] == ([] if expected else ["h051"])
    finally:
        await bus.close()
