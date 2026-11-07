# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 15: golden and error chain event tests for the delegation contracts, in memory and on a real broker (OMN-22977).

The chains are the ones generated from node_delegation_orchestrator's walker paths (the committed
``node_delegation_orchestrator.chains.json``); each is re-driven through the real handlers. The
in-memory leg runs everywhere. The real-broker leg publishes the same typed events through a Kafka
broker named in ``PIECE15_CHAIN_BOOTSTRAP`` (a plaintext lab broker the caller names for the proof,
never the dev lane's) and asserts the chain that comes back out of it:

    PIECE15_CHAIN_BOOTSTRAP=<host:port> uv run pytest tests/chains/test_piece15_chain.py -m kafka -q

Mutations (W19): ``drop_success_emit`` removes the success terminal from the bus and
``swallow_failure`` removes the failure terminal; every golden chain must go red under the first
and every error chain under the second.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_generation import (
    ModelGeneratedChain,
)
from tests.chains.chain_assert import (
    ChainAssertionError,
    ChainEvent,
    ChainRecorder,
    assert_chain,
    assert_error_chain,
)
from tests.chains.delegation.conftest import pinned_bifrost_contract
from tests.chains.generated.driver_delegation import DRIVERS
from tests.chains.generated.test_generated_chains import (
    _typed,
    committed,
    obligations,
)

__all__ = ["pinned_bifrost_contract"]

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("pinned_bifrost_contract", "stub_provider_quota_reader"),
]

OWNER = "node_delegation_orchestrator"
BOOTSTRAP_ENV = "PIECE15_CHAIN_BOOTSTRAP"
TIMEOUT_S = 30.0

# The named mutations: which event classes the broken emit never publishes.
MUTATIONS: dict[str, frozenset[str]] = {
    "drop_success_emit": frozenset(
        {"ModelDelegationCompleted", "ModelDelegationTerminalCompletedV2"}
    ),
    "swallow_failure": frozenset(
        {
            "ModelDelegationFailed",
            "ModelDelegationTerminalFailedRoutedV2",
            "ModelDelegationTerminalFailedUnroutedV2",
        }
    ),
}
# The chain kind each mutation must turn red.
MUTATION_KIND = {"drop_success_emit": "golden", "swallow_failure": "error"}


def chains() -> list[ModelGeneratedChain]:
    expectations = committed(OWNER)
    assert expectations is not None, "no committed chains for the delegation contract"
    return list(expectations.chains)


def chain_ids() -> list[str]:
    return [chain.path_id for chain in chains()]


def by_kind(kind: str) -> list[ModelGeneratedChain]:
    return [chain for chain in chains() if chain.kind == kind]


async def run_chain(chain: ModelGeneratedChain) -> tuple[uuid.UUID, object]:
    obligation = next(o for o in obligations(OWNER) if o.path_id == chain.path_id)
    return await DRIVERS[OWNER].run(obligation)


def check(chain: ModelGeneratedChain, cid: uuid.UUID, run: object) -> None:
    """The golden or error chain assertion over a run (a ChainRun)."""
    events = run.events  # type: ignore[attr-defined]
    assert_ = assert_error_chain if chain.kind == "error" else assert_chain
    assert_(
        events,
        expected_event_types=chain.expected_event_types,
        terminal_fields=_typed(events[-1].payload, chain.terminal_fields),
        correlation_id=cid,
        bus_history_count=run.bus_history_count,  # type: ignore[attr-defined]
    )


def test_the_delegation_contract_has_golden_and_error_chains() -> None:
    kinds = {chain.kind for chain in chains()}
    assert kinds == {"golden", "error"}
    # Every walker path of the contract has a chain; none is left to a handwritten test.
    assert sorted(chain_ids()) == sorted(o.path_id for o in obligations(OWNER))


@pytest.mark.parametrize("path_id", chain_ids())
async def test_chain_holds_on_the_in_memory_bus(path_id: str) -> None:
    chain = next(c for c in chains() if c.path_id == path_id)
    cid, run = await run_chain(chain)
    check(chain, cid, run)


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_mutation_turns_every_chain_of_its_kind_red(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = MUTATIONS[mutation]
    publish = ChainRecorder.publish

    async def broken_publish(
        self: ChainRecorder, topic: str, event: object, **kw: object
    ) -> None:
        if type(event).__name__ in dropped:
            return
        await publish(self, topic, event, **kw)  # type: ignore[arg-type]

    targets = by_kind(MUTATION_KIND[mutation])
    assert targets, f"no {MUTATION_KIND[mutation]} chain to mutate"
    # Control: the same chains are green before the mutation.
    for chain in targets:
        cid, run = await run_chain(chain)
        check(chain, cid, run)
    monkeypatch.setattr(ChainRecorder, "publish", broken_publish)
    for chain in targets:
        cid, run = await run_chain(chain)
        with pytest.raises(ChainAssertionError):
            check(chain, cid, run)


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_mutation_leaves_the_other_kind_of_chain_green(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutation is named for one terminal: it must not turn the other kind red by accident."""
    dropped = MUTATIONS[mutation]
    publish = ChainRecorder.publish

    async def broken_publish(
        self: ChainRecorder, topic: str, event: object, **kw: object
    ) -> None:
        if type(event).__name__ in dropped:
            return
        await publish(self, topic, event, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ChainRecorder, "publish", broken_publish)
    other = "error" if MUTATION_KIND[mutation] == "golden" else "golden"
    for chain in by_kind(other):
        if any(t in dropped for t in chain.expected_event_types):
            continue
        cid, run = await run_chain(chain)
        check(chain, cid, run)


class RedpandaCarry:
    """Publish a run's typed events through a real broker and read the chain back out of it."""

    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.topic = f"piece15-chain-{uuid.uuid4().hex}"

    async def carry(self, events: tuple[ChainEvent, ...]) -> list[tuple[str, str, str]]:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await producer.start()
        try:
            for event in events:
                await producer.send_and_wait(
                    self.topic,
                    key=str(event.correlation_id).encode(),
                    value=event.payload.model_dump_json().encode(),  # type: ignore[attr-defined]
                    headers=[("event_type", event.event_type.encode())],
                    partition=0,
                )
        finally:
            await producer.stop()
        consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap,
            security_protocol="PLAINTEXT",
            group_id=f"{self.topic}-reader",
            auto_offset_reset="earliest",
        )
        await consumer.start()
        read: list[tuple[str, str, str]] = []
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async for message in consumer:
                    headers = dict(message.headers)
                    read.append(
                        (
                            headers["event_type"].decode(),
                            message.key.decode(),
                            message.value.decode(),
                        )
                    )
                    if len(read) == len(events):
                        break
        finally:
            await consumer.stop()
        return read


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.parametrize("kind", ["golden", "error"])
async def test_chain_holds_on_the_real_broker(kind: str) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    for chain in by_kind(kind):
        cid, run = await run_chain(chain)
        events = run.events  # type: ignore[attr-defined]
        read = await RedpandaCarry(bootstrap).carry(events)
        # What the broker returned is the chain: same order, one correlation id, same payloads.
        assert [item[0] for item in read] == list(chain.expected_event_types)
        assert {item[1] for item in read} == {str(cid)}
        assert [item[2] for item in read] == [
            e.payload.model_dump_json()
            for e in events  # type: ignore[attr-defined]
        ]
        check(chain, cid, run)
