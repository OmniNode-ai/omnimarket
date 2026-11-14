# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 (OMN-23866): golden and error chains of the host disk workflow, on the in-memory bus and the real dev broker.

Golden: a scheduled run reads the readings, sends one command per decision, collects the results and records the run.
Error: a command the effect refuses, and a command the effect never answers before its deadline, each end in a recorded
run that names the failure. The dev broker half runs when HOST_DISK_CHAIN_LANE and HOST_DISK_CHAIN_ROOT name a declared
bus lane, as the host disk reading chain does.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskFailureReason,
    EnumHostDiskRunOutcome,
    ModelHostDiskRunCompleted,
)
from tests.nodes.node_host_disk_orchestrator.orchestrator_inputs import (
    HOSTS,
    T0,
    Orchestrator,
)

from .host_disk_chain import (
    DevBrokerWire,
    InMemoryWire,
    Wire,
    dev_broker_target,
    new_key,
    run_chain,
)

pytestmark = pytest.mark.unit


def _assert_golden(records: list[ModelHostDiskRunCompleted], key: str) -> None:
    [record] = records
    assert record.outcome is EnumHostDiskRunOutcome.COMPLETED
    assert record.trigger == "scheduled"
    assert record.practice_mode is True
    assert [r.host_name for r in record.readings] == list(HOSTS)
    assert record.decision is not None
    assert [c.host_name for c in record.decision.commands] == list(HOSTS)
    assert [c.idempotency_key for c in record.commands] == [
        f"{h}|uv_cache|{T0.isoformat()}" for h in HOSTS
    ]
    assert [r.status for r in record.results] == ["completed", "completed"]
    assert all(r.evicted_count == 2 for r in record.results)


def _assert_refused(records: list[ModelHostDiskRunCompleted], key: str) -> None:
    [record] = records
    assert record.outcome is EnumHostDiskRunOutcome.FAILED
    assert [r.failure_reason for r in record.results] == [
        EnumHostDiskFailureReason.REFUSED
    ] * len(HOSTS)


def _assert_silent(records: list[ModelHostDiskRunCompleted], key: str) -> None:
    [record] = records
    assert record.outcome is EnumHostDiskRunOutcome.FAILED
    assert [r.failure_reason for r in record.results] == [
        EnumHostDiskFailureReason.NO_RESULT_BY_DEADLINE
    ] * len(HOSTS)
    assert record.finished_at > record.started_at


async def _golden(wire: Wire) -> None:
    key = new_key()
    _assert_golden(await run_chain(wire, Orchestrator(), key), key)


async def _refused(wire: Wire) -> None:
    key = new_key()
    _assert_refused(await run_chain(wire, Orchestrator(), key, effect="refuse"), key)


async def _silent(wire: Wire) -> None:
    key = new_key()
    _assert_silent(await run_chain(wire, Orchestrator(), key, effect="silent"), key)


async def test_golden_chain_scheduled_run_to_recorded_result_in_memory() -> None:
    await _golden(InMemoryWire())


async def test_error_chain_command_refused_in_memory() -> None:
    await _refused(InMemoryWire())


async def test_error_chain_effect_silent_past_its_deadline_in_memory() -> None:
    await _silent(InMemoryWire())


@pytest.mark.kafka
@pytest.mark.integration
async def test_golden_chain_on_the_real_dev_broker() -> None:
    lane, root = dev_broker_target()
    async with DevBrokerWire(lane, root, new_key()) as wire:
        await _golden(wire)


@pytest.mark.kafka
@pytest.mark.integration
async def test_error_chain_command_refused_on_the_real_dev_broker() -> None:
    lane, root = dev_broker_target()
    async with DevBrokerWire(lane, root, new_key()) as wire:
        await _refused(wire)


@pytest.mark.kafka
@pytest.mark.integration
async def test_error_chain_effect_silent_on_the_real_dev_broker() -> None:
    lane, root = dev_broker_target()
    async with DevBrokerWire(lane, root, new_key()) as wire:
        await _silent(wire)
