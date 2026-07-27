# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14528: runtime_sweep deadness-detector class fix.

Proves the three seams that turned the consumer-liveness check from a blind
green-over-nothing shelf into a real, fail-closed deadness detector:

1. ``broker_probe.collect_live_consumer_groups`` collects the census IN CODE and
   counts only LIVE (non-Empty) groups — an ``Empty``/``Dead`` group (committed
   offsets, zero attached members) is a dead corpse and MUST NOT be reported as
   live, or the RED proof would go false-green.
2. ``NodeRuntimeSweep._resolve_default_input`` (the ``onex skill runtime_sweep``
   dispatch path) actually PROBES the broker and marks the census required —
   this is the path where the census used to be ``None`` forever, so the check
   silently never ran.
3. The per-contract identity matcher anchors on ``.{node}.consume.`` so a longer
   sibling name never yields a false match (no ``node_foo``-vouched-by-
   ``node_foobar`` prefix collision).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_runtime_sweep.broker_probe import (
    collect_live_consumer_groups,
)
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    EnumSweepCheck,
    ModelContractInput,
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)


def _group(group_id: str, state: str) -> SimpleNamespace:
    """A fake confluent-kafka ConsumerGroupListing (group_id + enum-like state)."""
    return SimpleNamespace(group_id=group_id, state=SimpleNamespace(name=state))


class _FakeFuture:
    def __init__(self, listing: object) -> None:
        self._listing = listing

    def result(self, timeout: float) -> object:
        return self._listing


class _FakeAdminClient:
    """Stand-in for confluent_kafka.admin.AdminClient returning a fixed listing."""

    listing: object = None

    def __init__(self, config: dict[str, object]) -> None:
        self._config = config

    def list_consumer_groups(self, request_timeout: float) -> _FakeFuture:
        return _FakeFuture(type(self).listing)


@pytest.mark.unit
class TestBrokerProbeLiveness:
    """The probe counts only LIVE groups; Empty/Dead corpses are excluded."""

    def _run_with_listing(self, listing: object) -> list[str]:
        _FakeAdminClient.listing = listing
        with patch("confluent_kafka.admin.AdminClient", _FakeAdminClient):
            return collect_live_consumer_groups("fakehost:19092")

    def test_empty_and_dead_groups_excluded(self) -> None:
        """An Empty/Dead/Unknown group is a corpse — never reported as live."""
        listing = SimpleNamespace(
            valid=[
                _group("dev.omnimarket.node_alive.consume.v1", "STABLE"),
                _group(
                    "dev.omnimarket.node_rebalancing.consume.v1",
                    "PREPARING_REBALANCING",
                ),
                _group("dev.omnimarket.node_dead_corpse.consume.v1", "EMPTY"),
                _group("dev.omnimarket.node_reaped.consume.v1", "DEAD"),
                _group("dev.omnimarket.node_unknown.consume.v1", "UNKNOWN"),
            ],
            errors=[],
        )
        live = self._run_with_listing(listing)
        assert live == [
            "dev.omnimarket.node_alive.consume.v1",
            "dev.omnimarket.node_rebalancing.consume.v1",
        ]
        # The Empty group's mere existence must NOT vouch for its node.
        assert not any("node_dead_corpse" in g for g in live)

    def test_broker_errors_fail_closed(self) -> None:
        """A partial/errored listing is not a census — the probe raises."""
        from omnibase_infra.errors import InfraConnectionError

        listing = SimpleNamespace(
            valid=[_group("dev.omnimarket.node_alive.consume.v1", "STABLE")],
            errors=[RuntimeError("coordinator unavailable")],
        )
        with pytest.raises(InfraConnectionError):
            self._run_with_listing(listing)


@pytest.mark.unit
class TestSkillPathResolverProbesBroker:
    """The skill dispatch path collects the census in code and requires it."""

    def test_resolver_probes_and_requires_census(self) -> None:
        """No-input (skill) request → resolver probes broker + sets require flag."""
        contracts = [
            ModelContractInput(
                node_name="node_probed",
                description="A real, sufficiently long node description here.",
                subscribe_topics=["onex.cmd.demo.do.v1"],
                runtime_profiles=["effects"],
            )
        ]
        fake_census = ["dev.omnimarket.node_probed.consume.v1"]
        captured: dict[str, str] = {}

        def fake_probe(bootstrap: str) -> list[str]:
            captured["bootstrap"] = bootstrap
            return fake_census

        with (
            patch.dict(
                os.environ,
                {
                    "OMNI_HOME": "/nonexistent",
                    "KAFKA_BOOTSTRAP_SERVERS": "fakehost:19092",
                },
            ),
            patch(
                "omnimarket.nodes.node_runtime_sweep.collection.collect_contracts",
                return_value=contracts,
            ),
            patch(
                "omnimarket.nodes.node_runtime_sweep.broker_probe.collect_live_consumer_groups",
                side_effect=fake_probe,
            ),
        ):
            resolved = NodeRuntimeSweep._resolve_default_input(RuntimeSweepRequest())

        assert captured.get("bootstrap") == "fakehost:19092"
        assert resolved.require_live_consumer_census is True
        assert resolved.live_consumer_groups == fake_census

    def test_resolver_fails_closed_without_broker(self) -> None:
        """No broker configured → the skill path raises, never silently skips."""
        contracts = [
            ModelContractInput(
                node_name="node_x",
                description="A real, sufficiently long node description here.",
                subscribe_topics=["onex.cmd.demo.do.v1"],
                runtime_profiles=["effects"],
            )
        ]
        with patch.dict(os.environ, {"OMNI_HOME": "/nonexistent"}, clear=False):
            os.environ.pop("KAFKA_BOOTSTRAP_SERVERS", None)
            with (
                patch(
                    "omnimarket.nodes.node_runtime_sweep.collection.collect_contracts",
                    return_value=contracts,
                ),
                pytest.raises(ValueError, match="KAFKA_BOOTSTRAP_SERVERS"),
            ):
                NodeRuntimeSweep._resolve_default_input(RuntimeSweepRequest())

    def test_resolver_skips_probe_when_liveness_disabled(self) -> None:
        """Scoping enabled_checks to exclude CONSUMER_LIVENESS skips the probe."""
        contracts = [
            ModelContractInput(
                node_name="node_x",
                description="A real, sufficiently long node description here.",
                subscribe_topics=["onex.cmd.demo.do.v1"],
                runtime_profiles=["effects"],
            )
        ]
        with patch.dict(os.environ, {"OMNI_HOME": "/nonexistent"}, clear=False):
            os.environ.pop(
                "KAFKA_BOOTSTRAP_SERVERS", None
            )  # no broker, but no probe either
            with patch(
                "omnimarket.nodes.node_runtime_sweep.collection.collect_contracts",
                return_value=contracts,
            ):
                resolved = NodeRuntimeSweep._resolve_default_input(
                    RuntimeSweepRequest(enabled_checks=[EnumSweepCheck.WIRING])
                )
        assert resolved.require_live_consumer_census is False
        assert resolved.live_consumer_groups is None


@pytest.mark.unit
class TestPerContractIdentityMatch:
    """The per-contract matcher anchors on the node-name segment (no collisions)."""

    def test_prefix_collision_does_not_vouch(self) -> None:
        """``node_foo`` must NOT be vouched for by ``node_foobar``'s live group."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="node_foo",
                    description="A real, sufficiently long node description here.",
                    subscribe_topics=["onex.cmd.demo.do.v1"],
                    runtime_profiles=["effects"],
                )
            ],
            live_consumer_groups=["dev.omnimarket.node_foobar.consume.v1"],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)
        assert result.by_type.get("CONTRACT_NO_LIVE_CONSUMER", 0) == 1

    def test_node_name_case_and_separator_normalized(self) -> None:
        """Match uses the runtime's normalization (case/separator-insensitive)."""
        handler = NodeRuntimeSweep()
        request = RuntimeSweepRequest(
            contracts=[
                ModelContractInput(
                    node_name="Node-Mixed_Case",
                    description="A real, sufficiently long node description here.",
                    subscribe_topics=["onex.cmd.demo.do.v1"],
                    runtime_profiles=["effects"],
                )
            ],
            # Runtime lower-cases node_name into the group id.
            live_consumer_groups=["dev.omnimarket.node-mixed_case.consume.v1"],
            require_live_consumer_census=True,
            enabled_checks=[EnumSweepCheck.CONSUMER_LIVENESS],
        )
        result = handler.handle(request)
        assert result.by_type.get("CONTRACT_NO_LIVE_CONSUMER", 0) == 0
