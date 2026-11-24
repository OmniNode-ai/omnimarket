# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1, AC3 and AC4 of OMN-26669: create, read back, retry and backfill the tenant wire topics."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_topic_provision_effect.handlers.handler_tenant_topic_provision import (
    HandlerTenantTopicProvision,
)
from omnimarket.nodes.node_tenant_topic_provision_effect.models.model_tenant_topic_provision import (
    EnumTenantTopicOutcome,
    EnumTenantTopicState,
    ModelTenantTopicProvisionCommand,
)
from tests.nodes.node_tenant_topic_provision_effect.fakes import (
    NAMES,
    TEMPLATE,
    FakeBroker,
    FakeMirrorSource,
    FakeRegistry,
    wire,
)

pytestmark = pytest.mark.unit


def _handler(
    broker: FakeBroker, registry: FakeRegistry, names: tuple[str, ...] = NAMES
) -> HandlerTenantTopicProvision:
    return HandlerTenantTopicProvision(
        mirror_source=FakeMirrorSource(names),
        registry=registry,
        lister=broker,
        creator=broker,
        topic_template=TEMPLATE,
        num_partitions=3,
        replication_factor=2,
    )


def _single(slug: str) -> ModelTenantTopicProvisionCommand:
    return ModelTenantTopicProvisionCommand(command_id=uuid4(), tenant_slug=slug)


def _backfill() -> ModelTenantTopicProvisionCommand:
    return ModelTenantTopicProvisionCommand(command_id=uuid4(), backfill=True)


# --- AC1 ---------------------------------------------------------------------------------------


async def test_ac1_creates_every_mirror_topic_under_the_tenant_prefix_and_reads_each_back() -> (
    None
):
    broker = FakeBroker()
    registry = FakeRegistry(("acme",))
    result = await _handler(broker, registry).handle(_single("acme"))
    assert result.outcome is EnumTenantTopicOutcome.PROVISIONED
    assert sorted(broker.live) == sorted(wire("acme"))
    (tenant,) = result.tenants
    assert tenant.state is EnumTenantTopicState.PROVISIONED
    assert sorted(tenant.created) == sorted(wire("acme"))
    assert tenant.read_back == tuple(sorted(wire("acme")))
    assert registry.states["acme"] is EnumTenantTopicState.PROVISIONED


async def test_ac1_creates_with_the_injected_partitions_and_replicas() -> None:
    broker = FakeBroker()
    await _handler(broker, FakeRegistry(("acme",))).handle(_single("acme"))
    assert {(p, r) for _, p, r in broker.creates} == {(3, 2)}


async def test_ac1_a_second_run_creates_none() -> None:
    broker = FakeBroker()
    registry = FakeRegistry(("acme",))
    handler = _handler(broker, registry)
    await handler.handle(_single("acme"))
    calls_after_first = len(broker.creates)
    second = await handler.handle(_single("acme"))
    assert len(broker.creates) == calls_after_first
    assert second.outcome is EnumTenantTopicOutcome.ALREADY_PROVISIONED
    assert second.tenants[0].created == ()
    assert second.tenants[0].state is EnumTenantTopicState.PROVISIONED


async def test_ac1_an_invalid_slug_is_refused_and_creates_nothing() -> None:
    broker = FakeBroker()
    result = await _handler(broker, FakeRegistry(())).handle(_single("Bad Slug!"))
    assert result.outcome is EnumTenantTopicOutcome.REFUSED_INVALID_SLUG
    assert broker.creates == []


def test_command_needs_exactly_one_of_slug_or_backfill() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ModelTenantTopicProvisionCommand(command_id=uuid4())
    with pytest.raises(ValueError, match="exactly one"):
        ModelTenantTopicProvisionCommand(
            command_id=uuid4(), tenant_slug="acme", backfill=True
        )


# --- AC3 ---------------------------------------------------------------------------------------


async def test_ac3_a_refused_create_names_the_topic_and_marks_the_tenant_unprovisioned() -> (
    None
):
    bad = wire("acme")[1]
    broker = FakeBroker(refuse=frozenset({bad}))
    registry = FakeRegistry(("acme",))
    result = await _handler(broker, registry).handle(_single("acme"))
    assert result.outcome is EnumTenantTopicOutcome.FAILED_TOPICS
    (tenant,) = result.tenants
    assert tenant.state is EnumTenantTopicState.UNPROVISIONED
    assert tenant.failed_topics == (bad,)
    assert bad in result.detail
    assert registry.states["acme"] is EnumTenantTopicState.UNPROVISIONED
    assert registry.failed["acme"] == (bad,)


async def test_ac3_a_partial_set_is_never_reported_as_done() -> None:
    bad = wire("acme")[0]
    broker = FakeBroker(refuse=frozenset({bad}))
    result = await _handler(broker, FakeRegistry(("acme",))).handle(_single("acme"))
    assert result.outcome is not EnumTenantTopicOutcome.PROVISIONED
    assert result.outcome is not EnumTenantTopicOutcome.ALREADY_PROVISIONED
    assert wire("acme")[1] in broker.live  # the topic that could be made was made


async def test_ac3_a_retry_completes_the_set_and_marks_it_provisioned() -> None:
    bad = wire("acme")[1]
    broker = FakeBroker(refuse=frozenset({bad}))
    registry = FakeRegistry(("acme",))
    handler = _handler(broker, registry)
    await handler.handle(_single("acme"))
    broker.refuse.clear()
    creates_before = len(broker.creates)
    retry = await handler.handle(_single("acme"))
    assert retry.outcome is EnumTenantTopicOutcome.PROVISIONED
    assert [name for name, _, _ in broker.creates[creates_before:]] == [bad]
    assert sorted(broker.live) == sorted(wire("acme"))
    assert registry.states["acme"] is EnumTenantTopicState.PROVISIONED


async def test_ac3_a_create_the_broker_accepted_but_does_not_list_is_a_failure() -> (
    None
):
    class Forgetful(FakeBroker):
        async def create_topic(
            self, name: str, num_partitions: int, replication_factor: int
        ) -> bool:
            self.creates.append((name, num_partitions, replication_factor))
            return True  # claims created; never holds it

    broker = Forgetful()
    registry = FakeRegistry(("acme",))
    result = await _handler(broker, registry).handle(_single("acme"))
    assert result.outcome is EnumTenantTopicOutcome.FAILED_TOPICS
    assert set(result.tenants[0].failed_topics) == set(wire("acme"))
    assert registry.states["acme"] is EnumTenantTopicState.UNPROVISIONED


async def test_ac3_an_unreachable_broker_is_a_typed_failure_with_the_tenant_unprovisioned() -> (
    None
):
    registry = FakeRegistry(("acme",))
    result = await _handler(FakeBroker(list_fails=True), registry).handle(
        _single("acme")
    )
    assert result.outcome is EnumTenantTopicOutcome.FAILED_BROKER_UNAVAILABLE
    assert registry.states["acme"] is EnumTenantTopicState.UNPROVISIONED


async def test_no_mirror_topics_is_refused_not_reported_as_provisioned() -> None:
    result = await _handler(FakeBroker(), FakeRegistry(("acme",)), names=()).handle(
        _single("acme")
    )
    assert result.outcome is EnumTenantTopicOutcome.FAILED_NO_MIRROR_TOPICS


# --- AC4 ---------------------------------------------------------------------------------------


async def test_ac4_backfill_creates_only_the_missing_topics_and_reports_per_tenant() -> (
    None
):
    have = wire("acme")[0]
    broker = FakeBroker(live=(have,))
    registry = FakeRegistry(("acme", "globex", "initech"))
    result = await _handler(broker, registry).handle(_backfill())
    assert {t.tenant_slug for t in result.tenants} == {"acme", "globex", "initech"}
    created_names = [name for name, _, _ in broker.creates]
    assert have not in created_names
    assert len(created_names) == len(set(created_names)) == 5
    by_slug = {t.tenant_slug: t for t in result.tenants}
    assert by_slug["acme"].created == (wire("acme")[1],)
    assert by_slug["acme"].existing == (have,)
    assert all(t.state is EnumTenantTopicState.PROVISIONED for t in result.tenants)
    assert result.outcome is EnumTenantTopicOutcome.PROVISIONED


async def test_ac4_backfill_never_deletes_and_a_second_backfill_creates_none() -> None:
    stray = "tenant-acme.onex.evt.synthetic.not-in-the-set.v1"
    broker = FakeBroker(live=(stray,))
    registry = FakeRegistry(("acme", "globex"))
    handler = _handler(broker, registry)
    await handler.handle(_backfill())
    creates_before = len(broker.creates)
    second = await handler.handle(_backfill())
    assert len(broker.creates) == creates_before
    assert broker.deletes == []
    assert stray in broker.live
    assert second.outcome is EnumTenantTopicOutcome.ALREADY_PROVISIONED
    assert all(t.created == () for t in second.tenants)


async def test_ac4_backfill_one_tenant_failing_does_not_stop_the_others() -> None:
    bad = wire("globex")[0]
    broker = FakeBroker(refuse=frozenset({bad}))
    registry = FakeRegistry(("acme", "globex", "initech"))
    result = await _handler(broker, registry).handle(_backfill())
    assert result.outcome is EnumTenantTopicOutcome.FAILED_TOPICS
    by_slug = {t.tenant_slug: t for t in result.tenants}
    assert by_slug["globex"].state is EnumTenantTopicState.UNPROVISIONED
    assert by_slug["globex"].failed_topics == (bad,)
    assert by_slug["acme"].state is EnumTenantTopicState.PROVISIONED
    assert by_slug["initech"].state is EnumTenantTopicState.PROVISIONED


async def test_ac3_the_error_class_of_a_refused_create_is_reported_without_its_message() -> (
    None
):
    bad = wire("acme")[1]
    broker = FakeBroker(refuse=frozenset({bad}))
    result = await _handler(broker, FakeRegistry(("acme",))).handle(_single("acme"))
    assert result.tenants[0].create_errors == (f"{bad}: PermissionError",)
    assert "not authorized" not in result.model_dump_json()
