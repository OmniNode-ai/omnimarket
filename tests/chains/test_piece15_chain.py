# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 15: golden and error chain event tests for the delegation contracts, in memory and on a real broker (OMN-22977).

The chains are the ones generated from node_delegation_orchestrator's walker paths (the committed
``node_delegation_orchestrator.chains.json``); each is re-driven through the real handlers. The
in-memory leg runs everywhere. The real-broker leg publishes the same typed events through a Kafka
broker named in ``PIECE15_CHAIN_BOOTSTRAP`` (a plaintext lab broker the caller names for the proof,
never the dev lane's) and asserts the chain that comes back out of it:

    PIECE15_CHAIN_BOOTSTRAP=<host:port> uv run pytest tests/chains/test_piece15_chain.py -m kafka -q

A proof run also sets ``PIECE15_REQUIRE_BROKER=1``: without a named broker the broker cases then fail
instead of skipping, so a green run means the broker cases ran.

Mutations (W19): ``drop_success_emit`` removes the success terminal from the bus and
``swallow_failure`` removes the failure terminal; every golden chain must go red under the first
and every error chain under the second.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegation_synthetic_probe_compute.handlers.handler_delegation_synthetic_probe import (
    load_rules,
)
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
#: When set, a missing broker is a failure, so a proof run cannot succeed on skipped broker cases.
REQUIRE_BROKER_ENV = "PIECE15_REQUIRE_BROKER"
TIMEOUT_S = 30.0
CONTRACT_FILE = Path(__file__).parents[2] / "contracts" / "OMN-22977.yaml"
PROOF_ID = "dod-omn22977-integration-proof"

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


async def run_chain(chain: ModelGeneratedChain) -> tuple[uuid.UUID, Any]:
    obligation = next(o for o in obligations(OWNER) if o.path_id == chain.path_id)
    return await DRIVERS[OWNER].run(obligation)


def check(chain: ModelGeneratedChain, cid: uuid.UUID, run: Any) -> None:
    """The golden or error chain assertion over a run (a ChainRun)."""
    events = run.events
    assert_ = assert_error_chain if chain.kind == "error" else assert_chain
    assert_(
        events,
        expected_event_types=chain.expected_event_types,
        terminal_fields=_typed(events[-1].payload, chain.terminal_fields),
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )


def test_the_delegation_contract_has_golden_and_error_chains() -> None:
    kinds = {chain.kind for chain in chains()}
    assert kinds == {"golden", "error"}
    # Every walker path of the contract has a chain; none is left to a handwritten test.
    assert sorted(chain_ids()) == sorted(o.path_id for o in obligations(OWNER))


def test_the_synthetic_probe_rules_load_beside_the_delegation_chains() -> None:
    """The probe node's contract rules load and its task completes on a golden chain of this contract."""
    assert load_rules().bound_seconds >= 1
    golden_events = {
        t for chain in by_kind("golden") for t in chain.expected_event_types
    }
    assert "ModelDelegationCompleted" in golden_events


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
        self: ChainRecorder, topic: str, event: Any, **kw: Any
    ) -> None:
        if type(event).__name__ in dropped:
            return
        await publish(self, topic, event, **kw)

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
        self: ChainRecorder, topic: str, event: Any, **kw: Any
    ) -> None:
        if type(event).__name__ in dropped:
            return
        await publish(self, topic, event, **kw)

    monkeypatch.setattr(ChainRecorder, "publish", broken_publish)
    other = "error" if MUTATION_KIND[mutation] == "golden" else "golden"
    for chain in by_kind(other):
        if any(t in dropped for t in chain.expected_event_types):
            continue
        cid, run = await run_chain(chain)
        check(chain, cid, run)


def broker_bootstrap() -> str:
    """The lab broker the caller named; absent, skip, or fail when the run requires a broker."""
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if bootstrap:
        return bootstrap
    message = f"{BOOTSTRAP_ENV} names no lab broker"
    if os.environ.get(REQUIRE_BROKER_ENV):
        pytest.fail(f"{message}, and {REQUIRE_BROKER_ENV} requires one")
    pytest.skip(f"Kafka not reachable: {message}")


def from_carried(run: Any, read: list[tuple[str, str, str]]) -> Any:
    """The run as the carrier returned it: type, key and payload of each event decoded from `read`."""
    if len(read) != len(run.events):
        raise ChainAssertionError(
            f"the carrier returned {len(read)} events; the run emitted {len(run.events)}"
        )
    events = tuple(
        replace(
            event,
            event_type=event_type,
            correlation_id=type(event.correlation_id)(key),
            payload=type(event.payload).model_validate_json(value),
        )
        for event, (event_type, key, value) in zip(run.events, read, strict=True)
    )
    return replace(run, events=events)


def identity_carry(events: tuple[ChainEvent, ...]) -> list[tuple[str, str, str]]:
    """What RedpandaCarry returns for a broker that delivers every event untouched."""
    return [
        (e.event_type, str(e.correlation_id), e.payload.model_dump_json())
        for e in events
    ]


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
                    value=event.payload.model_dump_json().encode(),
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
    bootstrap = broker_bootstrap()
    for chain in by_kind(kind):
        cid, run = await run_chain(chain)
        events = run.events
        read = await RedpandaCarry(bootstrap).carry(events)
        # What the broker returned is the chain: same order, one correlation id, same payloads.
        assert [item[0] for item in read] == list(chain.expected_event_types)
        assert {item[1] for item in read} == {str(cid)}
        assert [item[2] for item in read] == [
            e.payload.model_dump_json() for e in events
        ]
        # The assertion runs over what the broker returned, never the in-memory run.
        check(chain, cid, from_carried(run, read))


Carry = Callable[[tuple[ChainEvent, ...]], list[tuple[str, str, str]]]


def drop_last(events: tuple[ChainEvent, ...]) -> list[tuple[str, str, str]]:
    return identity_carry(events)[:-1]


def rename_terminal(events: tuple[ChainEvent, ...]) -> list[tuple[str, str, str]]:
    carried = identity_carry(events)
    return [*carried[:-1], ("ModelSomethingElse", *carried[-1][1:])]


def rekey_terminal(events: tuple[ChainEvent, ...]) -> list[tuple[str, str, str]]:
    carried = identity_carry(events)
    return [*carried[:-1], (carried[-1][0], str(uuid.uuid4()), carried[-1][2])]


@pytest.mark.parametrize("kind", ["golden", "error"])
async def test_the_chain_assertion_reads_what_the_carrier_returned(kind: str) -> None:
    """Carry fidelity decides the chain assertion: an untouched carry holds, a broken one goes red."""
    for chain in by_kind(kind):
        cid, run = await run_chain(chain)
        check(chain, cid, from_carried(run, identity_carry(run.events)))
        for broken in (drop_last, rename_terminal, rekey_terminal):
            with pytest.raises((ChainAssertionError, ValueError)):
                check(chain, cid, from_carried(run, broken(run.events)))


def test_a_missing_broker_fails_when_the_run_requires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(BOOTSTRAP_ENV, raising=False)
    monkeypatch.setenv(REQUIRE_BROKER_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        broker_bootstrap()


def test_a_missing_broker_skips_when_the_run_does_not_require_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(BOOTSTRAP_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_BROKER_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        broker_bootstrap()


def test_a_named_broker_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BOOTSTRAP_ENV, "broker.example:9092")
    monkeypatch.setenv(REQUIRE_BROKER_ENV, "1")
    assert broker_bootstrap() == "broker.example:9092"


def test_the_integration_proof_command_requires_the_broker_cases() -> None:
    """The contract's lab proof sets the require flag, so skipped broker cases cannot pass it."""
    contract = yaml.safe_load(CONTRACT_FILE.read_text(encoding="utf-8"))
    proof = next(item for item in contract["dod_evidence"] if item["id"] == PROOF_ID)
    command = proof["checks"][0]["check_value"]
    assert f"{REQUIRE_BROKER_ENV}=1" in command
    assert "test_piece15_chain.py" in command
