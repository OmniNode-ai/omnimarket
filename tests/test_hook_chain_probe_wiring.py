# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hook-chain probe is on the bus at both ends.

    onex.evt.platform.node-heartbeat.v1
      -> node_hook_chain_probe_trigger_effect   (throttled to the declared interval)
      -> onex.cmd.omnimarket.hook-chain-probe-requested.v1
      -> node_hook_chain_probe_effect
      -> onex.evt.omnimarket.hook-chain-probe-completed.v1 / -failed.v1
      -> node_hook_chain_probe_verdict_effect   (ERROR log on a broken chain)

Before this wiring the probe declared a command topic nothing published and two
terminal events nothing read, so hook-chain health was never probed unless an
operator ran it by hand.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_hook_chain_probe_effect.handlers.handler_hook_chain_probe import (
    HandlerHookChainProbe,
)
from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    ModelHookChainAddress,
    ModelHookChainProbeRequest,
)
from omnimarket.nodes.node_hook_chain_probe_trigger_effect.handlers.handler_hook_chain_probe_trigger import (
    HandlerHookChainProbeTrigger,
)
from omnimarket.nodes.node_hook_chain_probe_trigger_effect.models.model_hook_chain_probe_trigger import (
    ModelHookChainProbeHeartbeat,
)
from omnimarket.nodes.node_hook_chain_probe_verdict_effect.handlers.handler_hook_chain_probe_verdict import (
    HandlerHookChainProbeVerdict,
)
from omnimarket.nodes.node_hook_chain_probe_verdict_effect.models.model_hook_chain_probe_verdict import (
    ModelHookChainProbeOutcome,
)
from tests.test_golden_chain_hook_chain_probe_effect import (
    HOOK_TOPIC,
    STABILITY_LANE,
    _forwarder,
    _StubProbes,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_PROBE = _NODES / "node_hook_chain_probe_effect" / "contract.yaml"
_TRIGGER = _NODES / "node_hook_chain_probe_trigger_effect" / "contract.yaml"
_VERDICT = _NODES / "node_hook_chain_probe_verdict_effect" / "contract.yaml"

_HEARTBEAT = "onex.evt.platform.node-heartbeat.v1"
_COMMAND = "onex.cmd.omnimarket.hook-chain-probe-requested.v1"
_COMPLETED = "onex.evt.omnimarket.hook-chain-probe-completed.v1"
_FAILED = "onex.evt.omnimarket.hook-chain-probe-failed.v1"


def _contract(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _published(contract: dict[str, Any]) -> set[str]:
    bus = contract.get("event_bus") or {}
    return set(bus.get("publish_topics") or ())


def _subscribed(contract: dict[str, Any]) -> set[str]:
    bus = contract.get("event_bus") or {}
    return set(bus.get("subscribe_topics") or ())


def _all_contracts() -> list[dict[str, Any]]:
    return [_contract(p) for p in sorted(_NODES.glob("node_*/contract.yaml"))]


class TestTheProbeIsOnTheBusAtBothEnds:
    def test_the_command_topic_has_a_real_producer(self) -> None:
        producers = [c["name"] for c in _all_contracts() if _COMMAND in _published(c)]
        assert producers, f"nothing publishes {_COMMAND}"

    def test_the_probe_consumes_its_command_topic(self) -> None:
        assert _COMMAND in _subscribed(_contract(_PROBE))

    @pytest.mark.parametrize("topic", [_COMPLETED, _FAILED])
    def test_each_terminal_event_is_published_and_has_a_consumer(
        self, topic: str
    ) -> None:
        assert topic in _published(_contract(_PROBE))
        consumers = [c["name"] for c in _all_contracts() if topic in _subscribed(c)]
        assert consumers, f"nothing consumes {topic}"

    def test_the_public_dispatch_contract_is_unchanged(self) -> None:
        dispatch = _contract(_PROBE)["runtime_dispatch"]
        assert dispatch["command_topic"] == _COMMAND
        assert dispatch["terminal_events"] == {
            "success": _COMPLETED,
            "failure": _FAILED,
        }

    def test_the_trigger_rides_the_existing_heartbeat(self) -> None:
        trigger = _contract(_TRIGGER)
        assert _subscribed(trigger) == {_HEARTBEAT}
        assert _published(trigger) == {_COMMAND}

    def test_the_trigger_request_is_wire_compatible_with_the_probe_input(
        self,
    ) -> None:
        tick = HandlerHookChainProbeTrigger(
            clock=lambda: 10_000.0, interval_seconds=60
        ).handle(ModelHookChainProbeHeartbeat())
        assert tick is not None
        # The probe's own input model is the authority on the wire shape.
        parsed = ModelHookChainProbeRequest.model_validate(tick.model_dump(mode="json"))
        assert parsed.correlation_id is None


class TestTriggerThrottle:
    def test_first_tick_requests_a_probe_and_the_same_interval_does_not(self) -> None:
        now = [10_000.0]
        handler = HandlerHookChainProbeTrigger(
            clock=lambda: now[0], interval_seconds=900
        )
        assert handler.handle(ModelHookChainProbeHeartbeat()) is not None
        now[0] += 5
        assert handler.handle(ModelHookChainProbeHeartbeat()) is None

    def test_the_next_interval_requests_again(self) -> None:
        now = [10_000.0]
        handler = HandlerHookChainProbeTrigger(
            clock=lambda: now[0], interval_seconds=900
        )
        assert handler.handle(ModelHookChainProbeHeartbeat()) is not None
        now[0] += 900
        assert handler.handle(ModelHookChainProbeHeartbeat()) is not None

    def test_the_interval_is_contract_data(self) -> None:
        declared = _contract(_TRIGGER)["probe_schedule"]["probe_interval_seconds"]
        handler = HandlerHookChainProbeTrigger(clock=lambda: 0.0)
        assert handler.interval_seconds == declared

    def test_a_full_heartbeat_payload_is_accepted(self) -> None:
        ModelHookChainProbeHeartbeat.model_validate(
            {"service_name": "runtime", "uptime_seconds": 3, "memory_mb": 1.5}
        )


class TestVerdictConsumer:
    def test_a_broken_chain_is_logged_as_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        outcome = ModelHookChainProbeOutcome(
            correlation_id="c-1",
            chain_complete=False,
            failed_leg="forwarder_relay",
            primary_blocker="allowlist_denied",
        )
        with caplog.at_level(logging.ERROR):
            result = HandlerHookChainProbeVerdict().handle(outcome)
        assert result.healthy is False
        assert result.failed_leg == "forwarder_relay"
        assert any("forwarder_relay" in r.getMessage() for r in caplog.records)

    def test_a_complete_chain_is_healthy_and_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        outcome = ModelHookChainProbeOutcome(correlation_id="c-2", chain_complete=True)
        with caplog.at_level(logging.ERROR):
            result = HandlerHookChainProbeVerdict().handle(outcome)
        assert result.healthy is True
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_a_failed_event_with_no_verdict_is_never_healthy(self) -> None:
        # The -failed event carries an error, not a verdict; absence of
        # chain_complete must not read as green.
        outcome = ModelHookChainProbeOutcome.model_validate(
            {"correlation_id": "c-3", "error": "boom"}
        )
        assert HandlerHookChainProbeVerdict().handle(outcome).healthy is False


def _handler_for(contract: dict[str, Any]) -> Any:
    module = importlib.import_module(contract["handler"]["module"])
    return getattr(module, contract["handler"]["class"])


@pytest.mark.asyncio
class TestTheChainRunsOnABus:
    """Heartbeat in, health verdict out, over a bus, wired from the contracts only.

    Topics and handler classes are read from the three contracts; nothing here
    names a topic the contracts do not declare, so a contract edit that breaks a
    hop breaks this test.
    """

    async def test_a_heartbeat_reaches_a_recorded_verdict(self) -> None:
        trigger_c, probe_c, verdict_c = (
            _contract(_TRIGGER),
            _contract(_PROBE),
            _contract(_VERDICT),
        )
        bus = EventBusInmemory()
        await bus.start()
        trigger = HandlerHookChainProbeTrigger(clock=lambda: 10_000.0)
        probe = HandlerHookChainProbe(
            probes=_StubProbes(
                address=ModelHookChainAddress(
                    hook_topic=HOOK_TOPIC,
                    emit_lane=STABILITY_LANE,
                    emit_lane_authority="<test fixture>",
                    cloud_gateway_base_url="https://dev.api.omninode.ai",
                ),
                forwarder=_forwarder(),
            )
        )
        verdict = _handler_for(verdict_c)()
        assert isinstance(verdict, HandlerHookChainProbeVerdict)
        recorded: list[dict[str, Any]] = []

        (cmd_topic,) = _published(trigger_c)
        completed_topic = sorted(_published(probe_c))[0]
        (health_topic,) = _published(verdict_c)

        async def on_heartbeat(message: Any) -> None:
            out = trigger.handle(
                ModelHookChainProbeHeartbeat.model_validate_json(message.value)
            )
            if out is not None:
                await bus.publish(cmd_topic, None, out.model_dump_json().encode())

        async def on_command(message: Any) -> None:
            request = ModelHookChainProbeRequest.model_validate_json(message.value)
            result = await probe.handle(request)
            await bus.publish(completed_topic, None, result.model_dump_json().encode())

        async def on_outcome(message: Any) -> None:
            out = verdict.handle(
                ModelHookChainProbeOutcome.model_validate_json(message.value)
            )
            await bus.publish(health_topic, None, out.model_dump_json().encode())

        async def on_health(message: Any) -> None:
            recorded.append(json.loads(message.value))

        (heartbeat,) = _subscribed(trigger_c)
        await bus.subscribe(heartbeat, on_message=on_heartbeat, group_id="t")
        for topic in _subscribed(probe_c):
            await bus.subscribe(topic, on_message=on_command, group_id="p")
        for topic in _subscribed(verdict_c):
            await bus.subscribe(topic, on_message=on_outcome, group_id="v")
        await bus.subscribe(health_topic, on_message=on_health, group_id="h")

        await bus.publish(heartbeat, None, b'{"service_name": "runtime"}')
        await bus.publish(heartbeat, None, b'{"service_name": "runtime"}')

        assert len(recorded) == 1, "the second tick in one interval must not probe"
        assert recorded[0]["healthy"] is False
        assert recorded[0]["failed_leg"] == "forwarder_relay"
        assert recorded[0]["primary_blocker"] == "allowlist_denied"
        await bus.close()
