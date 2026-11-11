# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 16 (context, memory and evals): generated golden and error chain event tests, each shown red by a named mutation.

Plan section 3.2 and W19 (OMN-22982). Piece 16 holds two nodes whose contracts declare a chain:

* ``node_pilot_manifest_check_compute``: a check command arrives on the contract's command topic, the compute answers,
  and the answer is the one event on the contract's terminal topic;
* ``node_eval_corpus_reader_effect``: the J15 join (piece 15 to piece 16) answers on the floor's emission contract, a
  start then a success, or a typed ``upstream_`` failure that never started, all under one correlation id.

The scenarios are generated from those contracts (``generate_scenarios``): the topics, the terminal event, the failure
codes, the pieces and the statuses are read from the two ``contract.yaml`` files and the nodes' own enums, never spelled
in an assertion. Each scenario runs over a bus: the in-memory bus always, and a real broker of the declared test lane
when ``RUN_REDPANDA_PIECE16=1`` (nightly, on disposable topics tagged ``drill``). A backend that is asked for and cannot
be reached fails; nothing here skips.

``NAMED_MUTATIONS`` records every injected break with the chain it turns red; ``-k mutation`` runs them. Each is run
against the unbroken scenario first (the control), so the red is the break's and not the scenario's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import aiokafka
import pytest
import yaml
from aiokafka.admin import NewTopic
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.cli import delegate_lane_credentials as lane_credentials
from omnibase_infra.event_bus import kafka_auth

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.eval_corpus import (
    EnumCorpusCostStatus,
    EnumJoinJ15Outcome,
    ModelCorpusReadRequest,
    ModelDelegatedResult,
    ModelDelegationCost,
    ModelEvalCorpusEntry,
    ModelJoinJ15Request,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.handlers.handler_eval_corpus_reader import (
    HandlerEvalCorpusReader,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.handlers.handler_join_j15 import (
    HandlerJoinJ15,
    load_join_j15_policy,
)
from omnimarket.nodes.node_eval_corpus_reader_effect.protocols import (
    InMemoryDelegatedResults,
    InMemoryDelegationCosts,
    InMemoryEvalCorpus,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_check_finding import (
    EnumPilotCheckFinding,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_result import (
    ModelPilotManifestCheckResult,
)
from tests import bus_lane_guard
from tests.nodes.node_pilot_manifest_check_compute.support import (
    BAR,
    FROZEN_AT,
    STARTED_AT,
    frozen_manifest,
)
from tests.nodes.node_pilot_manifest_check_compute.support import (
    request as build_request,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"
PILOT_CONTRACT: dict[str, Any] = yaml.safe_load(
    (NODES / "node_pilot_manifest_check_compute" / "contract.yaml").read_text()
)
FLOOR_CONTRACT: dict[str, Any] = yaml.safe_load(
    (NODES / "node_floor_emission_compute" / "contract.yaml").read_text()
)
POLICY = load_join_j15_policy()

REDPANDA_FLAG = "RUN_REDPANDA_PIECE16"
NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
CHAIN_DEADLINE_S = 900
BUS_TIMEOUT_S = 15
DRILL_PARTITIONS = 1
DRILL_REPLICATION = 1

PILOT_COMMAND_TOPIC: str = PILOT_CONTRACT["runtime_dispatch"]["command_topic"]
PILOT_TERMINAL_TOPIC: str = PILOT_CONTRACT["terminal_event"]
# The floor's emission topics, told apart by the phase word in their contract names.
_FLOOR_TOPICS = FLOOR_CONTRACT["event_bus"]["subscribe_topics"]
EMISSION_TOPIC = {
    phase: next(t for t in _FLOOR_TOPICS if word in t)
    for phase, word in (
        ("start", "started"),
        ("success", "succeeded"),
        ("failure", "failed"),
    )
}


# --- the buses: the same one-message roundtrip on the in-memory bus and on a real broker --------------------------


class ProtocolChainBus(Protocol):
    name: str

    async def roundtrip(self, topic: str, payload: bytes) -> bytes: ...


class MemoryChainBus:
    name = "memory"

    async def roundtrip(self, topic: str, payload: bytes) -> bytes:
        bus = EventBusInmemory()
        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        async def on_message(message: Any) -> None:
            if not got.done():
                got.set_result(bytes(message.value))

        await bus.start()
        try:
            await bus.subscribe(topic, on_message=on_message, group_id="piece16-chain")
            await bus.publish(topic, key=None, value=payload)
            return await asyncio.wait_for(got, timeout=BUS_TIMEOUT_S)
        finally:
            await bus.close()


class RedpandaChainBus:
    """A real broker of the declared test lane, on disposable topics tagged ``drill``.

    The leg creates and deletes its own disposable topic through the admin client (the lane login
    needs CREATE and DELETE on the drill prefixes).
    """

    name = "redpanda"

    def __init__(self) -> None:
        from tests.bus_lane_guard import (
            assert_broker_in_declared_lane,
            resolve_test_bus_broker,
        )

        self._bootstrap = assert_broker_in_declared_lane(resolve_test_bus_broker())
        self._tag = f"drill-{uuid.uuid4().hex[:8]}"

    async def roundtrip(self, topic: str, payload: bytes) -> bytes:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_for,
        )
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import declared_test_lane

        # The declared lane's own identity (the machine's client store, else the SASL environment), bound for this
        # one broker address only; a lane that resolves no identity refuses here and the leg fails, never skips.
        _, transport = resolve_lane_binding(declared_test_lane())
        with contextlib.ExitStack() as scope:
            if transport is not None:
                scope.enter_context(bind_lane_client_transport(transport))
            auth = build_aiokafka_auth_kwargs_for(self._bootstrap)
        drill_topic = f"{topic}.{self._tag}"
        admin = AIOKafkaAdminClient(bootstrap_servers=self._bootstrap, **auth)
        await asyncio.wait_for(admin.start(), timeout=BUS_TIMEOUT_S)
        created = False
        try:
            # The consumer can only start on a topic that exists, so the drill topic is created first; a topic
            # error on the answer fails the leg, it never skips.
            response = await asyncio.wait_for(
                admin.create_topics(
                    [
                        NewTopic(
                            drill_topic,
                            num_partitions=DRILL_PARTITIONS,
                            replication_factor=DRILL_REPLICATION,
                        )
                    ]
                ),
                timeout=BUS_TIMEOUT_S,
            )
            for name, code, err in response.topic_errors:
                if code != 0:
                    raise RuntimeError(
                        f"topic {name!r} was refused with code {code}: {err}"
                    )
            created = True
            producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap, **auth)
            consumer = AIOKafkaConsumer(
                drill_topic,
                bootstrap_servers=self._bootstrap,
                group_id=f"piece16-{uuid.uuid4().hex[:8]}",
                auto_offset_reset="earliest",
                **auth,
            )
            await asyncio.wait_for(producer.start(), timeout=BUS_TIMEOUT_S)
            await asyncio.wait_for(consumer.start(), timeout=BUS_TIMEOUT_S)
            try:
                await producer.send_and_wait(drill_topic, value=payload)
                message = await asyncio.wait_for(
                    consumer.getone(), timeout=BUS_TIMEOUT_S
                )
                return bytes(message.value)
            finally:
                await consumer.stop()
                await producer.stop()
        finally:
            if created:
                await asyncio.wait_for(
                    admin.delete_topics([drill_topic]), timeout=BUS_TIMEOUT_S
                )
            await asyncio.wait_for(admin.close(), timeout=BUS_TIMEOUT_S)


BACKENDS = ["memory"] + (["redpanda"] if os.environ.get(REDPANDA_FLAG) == "1" else [])


def make_bus(backend: str) -> ProtocolChainBus:
    return MemoryChainBus() if backend == "memory" else RedpandaChainBus()


@pytest.fixture(params=BACKENDS)
def bus(request: pytest.FixtureRequest) -> ProtocolChainBus:
    return make_bus(request.param)


# --- scenarios, generated from the two contracts ------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    case_id: str
    kind: str  # "golden" or "error"
    chain: str  # "pilot" or "j15"
    expect: dict[str, Any] = field(default_factory=dict)


def generate_scenarios() -> tuple[Scenario, ...]:
    """One golden and the error faults of each chain, with expectations taken from the contracts and the enums."""
    status = EnumPilotStatus
    finding = EnumPilotCheckFinding
    pilot = (
        Scenario(
            "pilot-ready",
            "golden",
            "pilot",
            {"status": status.READY, "terminal_topic": PILOT_TERMINAL_TOPIC},
        ),
        Scenario(
            "pilot-accepted",
            "golden",
            "pilot",
            {"status": status.ACCEPTED, "terminal_topic": PILOT_TERMINAL_TOPIC},
        ),
        Scenario(
            "pilot-bar-and-hash-missing",
            "error",
            "pilot",
            {
                "status": status.REFUSED,
                "findings": (finding.BAR_MISSING, finding.HASH_MISSING),
            },
        ),
        Scenario(
            "pilot-hash-mismatch",
            "error",
            "pilot",
            {"status": status.REFUSED, "findings": (finding.HASH_MISMATCH,)},
        ),
        Scenario(
            "pilot-frozen-after-start",
            "error",
            "pilot",
            {"status": status.REFUSED, "findings": (finding.NOT_FROZEN_BEFORE_START,)},
        ),
        Scenario(
            "pilot-failed-bar-never-extended",
            "error",
            "pilot",
            {
                "status": status.REJECTED,
                "failed_metrics": ("overall_accuracy",),
                "extension_refused": True,
            },
        ),
        Scenario(
            "pilot-planted-recall-not-measured",
            "error",
            "pilot",
            {"status": status.REJECTED, "failed_metrics": ("planted_recall",)},
        ),
    )
    j15 = (
        Scenario(
            "j15-result-and-cost-land",
            "golden",
            "j15",
            {
                "phases": ["start", "success"],
                "counters": {"results_joined": 2, "entries_read": 2},
                "receipt": "golden_only",
            },
        ),
        Scenario(
            "j15-cost-missing-typed-not-dropped",
            "error",
            "j15",
            {
                "phases": ["failure"],
                "failure_code": POLICY.cost_missing_failure_code,
                "outcome": EnumJoinJ15Outcome.COST_MISSING,
                "receipt": "error_only",
            },
        ),
        Scenario(
            "j15-no-delegated-result",
            "error",
            "j15",
            {
                "phases": ["failure"],
                "failure_code": POLICY.no_result_failure_code,
                "outcome": EnumJoinJ15Outcome.REFUSED_NO_DELEGATED_RESULT,
                "receipt": "error_only",
            },
        ),
    )
    return pilot + j15


SCENARIOS = {s.case_id: s for s in generate_scenarios()}


def test_scenarios_are_generated_from_the_contracts_and_cover_both_kinds_of_each_chain() -> (
    None
):
    assert PILOT_CONTRACT["event_bus"]["publish_topics"] == [PILOT_TERMINAL_TOPIC]
    assert PILOT_CONTRACT["event_bus"]["subscribe_topics"] == [PILOT_COMMAND_TOPIC]
    assert set(EMISSION_TOPIC) == {"start", "success", "failure"}
    assert len(set(EMISSION_TOPIC.values())) == 3
    assert POLICY.cost_missing_failure_code.startswith("upstream_")
    assert POLICY.no_result_failure_code.startswith("upstream_")
    kinds = {(s.chain, s.kind) for s in SCENARIOS.values()}
    assert kinds == {
        ("pilot", "golden"),
        ("pilot", "error"),
        ("j15", "golden"),
        ("j15", "error"),
    }


# --- the pilot manifest check chain --------------------------------------------------------------------------------


GOOD_RESULTS = {"overall_accuracy": 0.9, "planted_recall": 1.0}


def pilot_request(case_id: str) -> ModelPilotManifestCheckRequest:
    """The fixture of one pilot scenario."""
    match case_id:
        case "pilot-ready":
            return build_request(frozen_manifest())
        case "pilot-accepted":
            return build_request(frozen_manifest(), results=GOOD_RESULTS)
        case "pilot-bar-and-hash-missing":
            return build_request(frozen_manifest(bar=(), bar_hash=None))
        case "pilot-hash-mismatch":
            return build_request(frozen_manifest(bar_hash="0" * 64))
        case "pilot-frozen-after-start":
            return build_request(
                frozen_manifest(frozen_at=STARTED_AT + timedelta(hours=1))
            )
        case "pilot-failed-bar-never-extended":
            return build_request(
                frozen_manifest(),
                results={"overall_accuracy": 0.5, "planted_recall": 1.0},
                extension_requested=True,
            )
        case "pilot-planted-recall-not-measured":
            return build_request(frozen_manifest(), results={"overall_accuracy": 0.9})
    raise AssertionError(case_id)


class PilotHandler(Protocol):
    def handle(
        self, request: ModelPilotManifestCheckRequest
    ) -> ModelPilotManifestCheckResult: ...


async def run_pilot_chain(
    bus: ProtocolChainBus,
    req: ModelPilotManifestCheckRequest,
    handler: PilotHandler | None = None,
    *,
    publish_terminal: bool = True,
) -> list[bytes]:
    """Publish the command, run the handler on what the bus delivered, publish its answer; return the terminal events."""
    delivered = await bus.roundtrip(PILOT_COMMAND_TOPIC, req.model_dump_json().encode())
    result = (handler or HandlerPilotManifestCheck()).handle(
        ModelPilotManifestCheckRequest.model_validate_json(delivered)
    )
    if not publish_terminal:
        return []
    return [
        await bus.roundtrip(PILOT_TERMINAL_TOPIC, result.model_dump_json().encode())
    ]


def assert_pilot_chain(
    scenario: Scenario,
    events: list[bytes],
    req: ModelPilotManifestCheckRequest,
) -> None:
    """The one assertion of both kinds: exactly one terminal event, under the command's id, with the scenario's answer."""
    assert len(events) == 1, f"{len(events)} terminal events, expected 1"
    result = ModelPilotManifestCheckResult.model_validate_json(events[0])
    expect = scenario.expect
    assert result.correlation_id == req.correlation_id
    assert result.status is expect["status"], result
    if scenario.kind == "golden":
        assert result.findings == ()
        assert result.failed_metrics == ()
        assert result.check_passed is True
        return
    if expect["status"] is EnumPilotStatus.REFUSED:
        assert result.check_passed is False
        assert result.findings == expect["findings"]
    else:
        assert result.check_passed is True
        assert result.failed_metrics == expect["failed_metrics"]
        assert result.extension_refused is expect.get("extension_refused", False)


@pytest.mark.parametrize(
    "case_id",
    [c for c, s in SCENARIOS.items() if s.chain == "pilot" and s.kind == "golden"],
)
def test_golden_chain_pilot_manifest_check(bus: ProtocolChainBus, case_id: str) -> None:
    req = pilot_request(case_id)
    events = asyncio.run(run_pilot_chain(bus, req))
    assert_pilot_chain(SCENARIOS[case_id], events, req)


@pytest.mark.parametrize(
    "case_id",
    [c for c, s in SCENARIOS.items() if s.chain == "pilot" and s.kind == "error"],
)
def test_error_chain_pilot_manifest_check(bus: ProtocolChainBus, case_id: str) -> None:
    req = pilot_request(case_id)
    events = asyncio.run(run_pilot_chain(bus, req))
    assert_pilot_chain(SCENARIOS[case_id], events, req)


def test_error_chain_pilot_malformed_command_never_reaches_the_terminal_topic(
    bus: ProtocolChainBus,
) -> None:
    from pydantic import ValidationError

    broken = (
        pilot_request("pilot-ready").model_dump_json().replace('"pilot"', '"pilote"')
    )
    published: list[bytes] = []

    async def chain() -> None:
        delivered = await bus.roundtrip(PILOT_COMMAND_TOPIC, broken.encode())
        ModelPilotManifestCheckRequest.model_validate_json(delivered)
        published.append(delivered)

    with pytest.raises(ValidationError):
        asyncio.run(chain())
    assert published == []


# --- the J15 join chain ----------------------------------------------------------------------------------------


class _Clock:
    """Moves one second per read, so a chain's events keep their order."""

    def __init__(self) -> None:
        self._t = NOW

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _result(run_id: str) -> ModelDelegatedResult:
    return ModelDelegatedResult(
        run_id=run_id,
        task_class="document",
        model_id="lab-qwen",
        outcome="completed",
        result_summary=f"summary of {run_id}",
    )


def _cost(run_id: str, usd: str = "0.0042") -> ModelDelegationCost:
    return ModelDelegationCost(
        run_id=run_id,
        cost_usd=Decimal(usd),
        input_tokens=1200,
        output_tokens=340,
        measurement_source="receipt",
    )


class _DroppingCorpus(InMemoryEvalCorpus):
    """Injected break: silently drops every entry that has no cost row."""

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        if entry.cost_status is EnumCorpusCostStatus.MISSING:
            return
        super().append(entry)


class _NeverWrittenCorpus(InMemoryEvalCorpus):
    """Injected break: accepts every append and keeps nothing."""

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        return None


class _InventingCosts(InMemoryDelegationCosts):
    """Injected break: answers a zero cost row for a run that has none."""

    def read_cost(self, run_id: str) -> ModelDelegationCost | None:
        return super().read_cost(run_id) or _cost(run_id, "0")


@dataclass
class J15Fixture:
    request: ModelJoinJ15Request
    results: list[ModelDelegatedResult]
    costs: list[ModelDelegationCost]
    upstream_ok: bool  # piece 15's own chain succeeded for these runs


def j15_fixture(case_id: str) -> J15Fixture:
    cid = uuid.uuid4().hex
    match case_id:
        case "j15-result-and-cost-land":
            runs = ("run-1", "run-2")
            return J15Fixture(
                ModelJoinJ15Request(correlation_id=cid, run_ids=runs),
                [_result(r) for r in runs],
                [_cost(r) for r in runs],
                True,
            )
        case "j15-cost-missing-typed-not-dropped":
            runs = ("run-1", "run-2")
            return J15Fixture(
                ModelJoinJ15Request(correlation_id=cid, run_ids=runs),
                [_result(r) for r in runs],
                [_cost("run-1")],
                False,
            )
        case "j15-no-delegated-result":
            return J15Fixture(
                ModelJoinJ15Request(correlation_id=cid, run_ids=("run-1", "run-9")),
                [_result("run-1")],
                [_cost("run-1")],
                False,
            )
    raise AssertionError(case_id)


@dataclass
class J15Run:
    carried: list[ModelNodeEmission]
    outcome: EnumJoinJ15Outcome
    corpus: InMemoryEvalCorpus
    correlation_id: str


async def run_j15_chain(
    bus: ProtocolChainBus,
    case_id: str,
    *,
    corpus: InMemoryEvalCorpus | None = None,
    costs_cls: type[InMemoryDelegationCosts] = InMemoryDelegationCosts,
    drop_phase: str | None = None,
    relabel_success: bool = False,
) -> J15Run:
    """Run piece 15's answer and the join, carry every emission over the bus, return what came back off it."""
    fixture = j15_fixture(case_id)
    cid = fixture.request.correlation_id
    corpus = corpus if corpus is not None else InMemoryEvalCorpus()
    sink = InMemoryEmissionSink()
    clock = _Clock()
    upstream = ChainEmitter(
        sink,
        node="node_delegation_orchestrator",
        piece=POLICY.from_piece,
        clock=clock,
        correlation_id=cid,
        backend="in_memory",
    )
    upstream.start()
    if fixture.upstream_ok:
        upstream.success({"results": 1, "costs": 1})
    else:
        upstream.failure(
            "delegation_incomplete", "a run with no delegated result or no cost row"
        )
    join = HandlerJoinJ15(
        results=InMemoryDelegatedResults(fixture.results),
        costs=costs_cls(fixture.costs),
        corpus=corpus,
        sink=sink,
        policy=POLICY,
        clock=clock,
        backend="in_memory",
    )
    result = join.handle(fixture.request)
    carried: list[ModelNodeEmission] = []
    for event in sink.events:
        if drop_phase and event.piece == POLICY.to_piece and event.phase == drop_phase:
            continue
        if (
            relabel_success
            and event.piece == POLICY.to_piece
            and event.phase == "success"
        ):
            event = event.model_copy(update={"correlation_id": uuid.uuid4().hex})
        raw = await bus.roundtrip(
            EMISSION_TOPIC[event.phase], event.model_dump_json().encode()
        )
        carried.append(ModelNodeEmission.model_validate(json.loads(raw)))
    return J15Run(carried, result.outcome, corpus, cid)


def fold_receipt(emissions: list[ModelNodeEmission]) -> str:
    """The floor's own reading of the join from the emissions read back off the bus."""
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=60),
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="delegation to context and evals",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    return next(j for j in receipts.joins if j.join == POLICY.join_id).state


def assert_j15_chain(scenario: Scenario, run: J15Run) -> None:
    """Both kinds: piece 16's emissions in order under piece 15's correlation id, the floor's reading, the corpus."""
    expect = scenario.expect
    mine = [e for e in run.carried if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == expect["phases"], [e.phase for e in mine]
    assert {e.correlation_id for e in mine} == {run.correlation_id}
    assert fold_receipt(run.carried) == expect["receipt"]
    if scenario.kind == "golden":
        assert run.outcome is EnumJoinJ15Outcome.LANDED
        assert mine[-1].counters == expect["counters"]
        assert mine[-1].failure_code is None
        read = HandlerEvalCorpusReader(corpus=run.corpus).handle(
            ModelCorpusReadRequest(run_ids=("run-1", "run-2"))
        )
        assert read.absent_run_ids == ()
        assert read.missing_cost_run_ids == ()
        return
    assert run.outcome is expect["outcome"]
    assert mine[-1].failure_code == expect["failure_code"]
    if expect["outcome"] is EnumJoinJ15Outcome.COST_MISSING:
        # Nothing dropped: both runs are in the corpus, and the one without a cost row is typed missing.
        read = HandlerEvalCorpusReader(corpus=run.corpus).handle(
            ModelCorpusReadRequest(run_ids=("run-1", "run-2"))
        )
        assert read.absent_run_ids == ()
        assert read.missing_cost_run_ids == ("run-2",)
        by_run = {e.run_id: e for e in read.entries}
        assert by_run["run-2"].cost_status is EnumCorpusCostStatus.MISSING
        assert by_run["run-2"].cost is None


@pytest.mark.parametrize(
    "case_id",
    [c for c, s in SCENARIOS.items() if s.chain == "j15" and s.kind == "golden"],
)
def test_golden_chain_join_j15(bus: ProtocolChainBus, case_id: str) -> None:
    assert_j15_chain(SCENARIOS[case_id], asyncio.run(run_j15_chain(bus, case_id)))


@pytest.mark.parametrize(
    "case_id",
    [c for c, s in SCENARIOS.items() if s.chain == "j15" and s.kind == "error"],
)
def test_error_chain_join_j15(bus: ProtocolChainBus, case_id: str) -> None:
    assert_j15_chain(SCENARIOS[case_id], asyncio.run(run_j15_chain(bus, case_id)))


# --- the named mutations -------------------------------------------------------------------------------------------


class _AcceptedReadAsReady:
    """Injected break: an accepted pilot is answered as ready, never as accepted."""

    def handle(
        self, request: ModelPilotManifestCheckRequest
    ) -> ModelPilotManifestCheckResult:
        result = HandlerPilotManifestCheck().handle(request)
        if result.status is EnumPilotStatus.ACCEPTED:
            return result.model_copy(update={"status": EnumPilotStatus.READY})
        return result


class _ExtensionAllowed:
    """Injected break: a failed bar's extension request is granted, so the refusal is not recorded."""

    def handle(
        self, request: ModelPilotManifestCheckRequest
    ) -> ModelPilotManifestCheckResult:
        result = HandlerPilotManifestCheck().handle(request)
        return result.model_copy(update={"extension_refused": False})


@dataclass(frozen=True)
class Mutation:
    kind: str  # the chain assertion it turns red: "golden" or "error"
    chain: str
    case_id: str
    break_chain: Callable[[ProtocolChainBus], Any]
    note: str


def _pilot_break(
    case_id: str,
    handler: PilotHandler | None = None,
    publish_terminal: bool = True,
) -> Callable[[ProtocolChainBus], Any]:
    def run(bus: ProtocolChainBus) -> Any:
        req = pilot_request(case_id)
        events = asyncio.run(
            run_pilot_chain(bus, req, handler, publish_terminal=publish_terminal)
        )
        assert_pilot_chain(SCENARIOS[case_id], events, req)

    return run


def _j15_break(case_id: str, **options: Any) -> Callable[[ProtocolChainBus], Any]:
    def run(bus: ProtocolChainBus) -> Any:
        assert_j15_chain(
            SCENARIOS[case_id], asyncio.run(run_j15_chain(bus, case_id, **options))
        )

    return run


MUTATIONS: dict[str, Mutation] = {
    "pilot-terminal-event-not-published": Mutation(
        "golden",
        "pilot",
        "pilot-accepted",
        _pilot_break("pilot-accepted", publish_terminal=False),
        "the answer is computed and never published on the terminal topic",
    ),
    "pilot-accepted-answered-as-ready": Mutation(
        "golden",
        "pilot",
        "pilot-accepted",
        _pilot_break("pilot-accepted", handler=_AcceptedReadAsReady()),
        "a pilot that met its bar never reaches accepted",
    ),
    "pilot-frozen-before-start-not-required": Mutation(
        "error",
        "pilot",
        "pilot-frozen-after-start",
        _pilot_break(
            "pilot-frozen-after-start",
            handler=HandlerPilotManifestCheck(
                ModelPilotManifestCheckOverlay(require_frozen_before_start=False)
            ),
        ),
        "a bar frozen after the start is let through",
    ),
    "pilot-missing-metric-passes-the-bar": Mutation(
        "error",
        "pilot",
        "pilot-planted-recall-not-measured",
        _pilot_break(
            "pilot-planted-recall-not-measured",
            handler=HandlerPilotManifestCheck(
                ModelPilotManifestCheckOverlay(missing_metric_fails_bar=False)
            ),
        ),
        "an unmeasured planted recall counts as met",
    ),
    "pilot-extension-of-a-failed-bar-granted": Mutation(
        "error",
        "pilot",
        "pilot-failed-bar-never-extended",
        _pilot_break("pilot-failed-bar-never-extended", handler=_ExtensionAllowed()),
        "the refused extension is not recorded on the result",
    ),
    "j15-start-event-dropped": Mutation(
        "golden",
        "j15",
        "j15-result-and-cost-land",
        _j15_break("j15-result-and-cost-land", drop_phase="start"),
        "piece 16's chain reaches the bus without its start",
    ),
    "j15-success-correlation-id-lost": Mutation(
        "golden",
        "j15",
        "j15-result-and-cost-land",
        _j15_break("j15-result-and-cost-land", relabel_success=True),
        "the success is published under a fresh correlation id",
    ),
    "j15-corpus-keeps-nothing": Mutation(
        "golden",
        "j15",
        "j15-result-and-cost-land",
        _j15_break("j15-result-and-cost-land", corpus=_NeverWrittenCorpus()),
        "the join appends and the corpus reader finds nothing",
    ),
    "j15-corpus-drops-entries-without-cost": Mutation(
        "error",
        "j15",
        "j15-cost-missing-typed-not-dropped",
        _j15_break("j15-cost-missing-typed-not-dropped", corpus=_DroppingCorpus()),
        "a result with no cost row is silently dropped from the corpus",
    ),
    "j15-cost-source-invents-a-zero-cost": Mutation(
        "error",
        "j15",
        "j15-cost-missing-typed-not-dropped",
        _j15_break("j15-cost-missing-typed-not-dropped", costs_cls=_InventingCosts),
        "a missing cost row is answered as a zero cost, so nothing reads missing",
    ),
    "j15-failure-event-dropped": Mutation(
        "error",
        "j15",
        "j15-no-delegated-result",
        _j15_break("j15-no-delegated-result", drop_phase="failure"),
        "the typed failure never reaches the bus: a silent end",
    ),
}

#: The named mutations, each with the chain assertion it turns red and the test that shows it.
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    name: (
        m.kind,
        f"test_mutation_turns_the_{m.kind}_chain_red[{name}]",
    )
    for name, m in MUTATIONS.items()
}


def _ids(kind: str) -> list[str]:
    return [n for n, m in MUTATIONS.items() if m.kind == kind]


@pytest.mark.parametrize("name", _ids("golden"))
def test_mutation_turns_the_golden_chain_red(bus: ProtocolChainBus, name: str) -> None:
    mutation = MUTATIONS[name]
    assert SCENARIOS[mutation.case_id].kind == "golden"
    # The control: the same scenario unbroken passes, so the red below is the break's.
    _CONTROLS[mutation.chain](bus, mutation.case_id)
    with pytest.raises(AssertionError):
        mutation.break_chain(bus)


@pytest.mark.parametrize("name", _ids("error"))
def test_mutation_turns_the_error_chain_red(bus: ProtocolChainBus, name: str) -> None:
    mutation = MUTATIONS[name]
    assert SCENARIOS[mutation.case_id].kind == "error"
    _CONTROLS[mutation.chain](bus, mutation.case_id)
    with pytest.raises(AssertionError):
        mutation.break_chain(bus)


def _control_pilot(bus: ProtocolChainBus, case_id: str) -> None:
    req = pilot_request(case_id)
    assert_pilot_chain(SCENARIOS[case_id], asyncio.run(run_pilot_chain(bus, req)), req)


def _control_j15(bus: ProtocolChainBus, case_id: str) -> None:
    assert_j15_chain(SCENARIOS[case_id], asyncio.run(run_j15_chain(bus, case_id)))


_CONTROLS: dict[str, Callable[[ProtocolChainBus, str], None]] = {
    "pilot": _control_pilot,
    "j15": _control_j15,
}


def test_mutation_record_names_a_golden_and_an_error_break_for_each_chain() -> None:
    covered = {(m.chain, m.kind) for m in MUTATIONS.values()}
    assert covered == {
        ("pilot", "golden"),
        ("pilot", "error"),
        ("j15", "golden"),
        ("j15", "error"),
    }
    assert all(
        _ids(kind) and name in NAMED_MUTATIONS
        for kind in ("golden", "error")
        for name in _ids(kind)
    )


def test_mutation_names_are_unique_and_each_one_has_a_note() -> None:
    assert len(MUTATIONS) == len(set(MUTATIONS))
    assert all(m.note for m in MUTATIONS.values())
    # Referenced here so the frozen-sample fixtures the builders share stay part of the pilot scenarios.
    assert FROZEN_AT < STARTED_AT
    assert len(BAR) == 2


# The drill-topic provision tests (no broker). The one address here: the broker the fakes are pointed at, never reached.
BROKER = "broker.invalid:1"
TOPIC = "omnimarket.pilot.command"
PAYLOAD = b"piece-16 drill payload"
TOPIC_AUTHORIZATION_FAILED = 29


class ClusterState:
    """The shared memory of the fakes: the events in call order, the one message in flight, the created topics."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.sent: bytes | None = None
        self.created: set[str] = set()
        self.refuse_create = False


STATE = ClusterState()


class FakeCreateResponse:
    def __init__(self, topic_errors: list[tuple[str, int, str]]) -> None:
        self.topic_errors = topic_errors


class FakeAdmin:
    def __init__(self, bootstrap_servers: str = "", **auth: object) -> None: ...

    async def start(self) -> None:
        STATE.events.append("admin.start")

    async def create_topics(self, topics: list[NewTopic]) -> FakeCreateResponse:
        errors: list[tuple[str, int, str]] = []
        for topic in topics:
            STATE.events.append(f"admin.create:{topic.name}")
            if STATE.refuse_create:
                code, err = TOPIC_AUTHORIZATION_FAILED, "TOPIC_AUTHORIZATION_FAILED"
                errors.append((topic.name, code, err))
            else:
                STATE.created.add(topic.name)
        return FakeCreateResponse(errors)

    async def delete_topics(self, topics: list[str]) -> None:
        for topic in topics:
            STATE.events.append(f"admin.delete:{topic}")

    async def close(self) -> None:
        STATE.events.append("admin.close")


class FakeProducer:
    def __init__(self, bootstrap_servers: str = "", **auth: object) -> None: ...

    async def start(self) -> None:
        STATE.events.append("producer.start")

    async def send_and_wait(self, topic: str, value: bytes) -> None:
        STATE.events.append(f"producer.send:{topic}")
        STATE.sent = value

    async def stop(self) -> None:
        STATE.events.append("producer.stop")


class FakeConsumer:
    """A fake broker model: a consumer can only start on a topic the admin has created earlier."""

    def __init__(
        self, *topics: str, bootstrap_servers: str = "", **auth: object
    ) -> None:
        self._topics = topics

    async def start(self) -> None:
        if any(topic in STATE.created for topic in self._topics):
            STATE.events.append("consumer.start")
            return
        raise TimeoutError(f"topic not found in cluster metadata: {self._topics[0]!r}")

    async def getone(self) -> types.SimpleNamespace:
        STATE.events.append("consumer.getone")
        if STATE.sent is None:
            raise AssertionError("getone before any send")
        return types.SimpleNamespace(value=STATE.sent)

    async def stop(self) -> None:
        STATE.events.append("consumer.stop")


def install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    for (module, name), value in (
        ((aiokafka, "AIOKafkaProducer"), FakeProducer),
        ((aiokafka, "AIOKafkaConsumer"), FakeConsumer),
        ((aiokafka.admin, "AIOKafkaAdminClient"), FakeAdmin),
        ((lane_credentials, "resolve_lane_binding"), lambda _lane: (None, None)),
        ((kafka_auth, "build_aiokafka_auth_kwargs_for"), lambda _bootstrap: {}),
        ((bus_lane_guard, "resolve_test_bus_broker"), lambda: BROKER),
        ((bus_lane_guard, "assert_broker_in_declared_lane"), lambda broker: broker),
    ):
        monkeypatch.setattr(module, name, value)
    STATE.events.clear()
    STATE.sent, STATE.created, STATE.refuse_create = None, set(), False


def test_golden_the_topic_is_created_before_the_consumer_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    got = asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert got == PAYLOAD
    events = STATE.events
    drill_topic = events[1].split("admin.create:", 1)[1]
    assert events == [
        "admin.start",
        f"admin.create:{drill_topic}",
        "producer.start",
        "consumer.start",
        f"producer.send:{drill_topic}",
        "consumer.getone",
        "consumer.stop",
        "producer.stop",
        f"admin.delete:{drill_topic}",
        "admin.close",
    ]


def test_error_a_refused_create_fails_the_leg_and_starts_no_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    STATE.refuse_create = True
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert "29" in str(exc.value)
    events = STATE.events
    assert "producer.start" not in events
    assert "consumer.start" not in events
    assert not any(event.startswith("admin.delete") for event in events)
    assert len(events) == 3
    assert events[0] == "admin.start"
    assert events[2] == "admin.close"
    assert events[1].startswith("admin.create:")


async def old_order_roundtrip(topic: str, payload: bytes) -> bytes:
    admin = FakeAdmin(bootstrap_servers=BROKER)
    producer = FakeProducer(bootstrap_servers=BROKER)
    consumer = FakeConsumer(topic, bootstrap_servers=BROKER)
    await admin.start()
    await producer.start()
    await consumer.start()
    await producer.send_and_wait(topic, value=payload)
    message = await consumer.getone()
    await consumer.stop()
    await producer.stop()
    await admin.close()
    return bytes(message.value)


def test_control_without_the_create_the_consumer_waits_on_a_missing_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fakes(monkeypatch)
    with pytest.raises(TimeoutError):
        asyncio.run(old_order_roundtrip(TOPIC, PAYLOAD))
    assert "consumer.start" not in STATE.events
    STATE.events.clear()
    got = asyncio.run(RedpandaChainBus().roundtrip(TOPIC, PAYLOAD))
    assert got == PAYLOAD
    create = next(event for event in STATE.events if event.startswith("admin.create:"))
    assert STATE.events.index(create) < STATE.events.index("consumer.start")


# The seam with the overlays repository: the five topics the leg sends on, spelled here and in that repository's own
# test (tests/test_broker_acl_overlays.py, PIECE16_DRILL_PREFIXES), whose rows give the lab logins CREATE and DELETE on
# each name plus the per-run drill suffix. A change to a contract topic fails this file; a change to the declared rows
# fails the overlays test.
DECLARED_DRILL_TOPICS = {
    "onex.cmd.omnimarket.pilot-manifest-check-requested.v1",
    "onex.evt.omnimarket.pilot-manifest-checked.v1",
    "onex.evt.omnimarket.floor-node-started.v1",
    "onex.evt.omnimarket.floor-node-succeeded.v1",
    "onex.evt.omnimarket.floor-node-failed.v1",
}


def test_seam_the_topics_the_leg_sends_on_are_the_ones_the_overlay_rows_declare() -> (
    None
):
    used = {PILOT_COMMAND_TOPIC, PILOT_TERMINAL_TOPIC, *EMISSION_TOPIC.values()}
    assert used == DECLARED_DRILL_TOPICS
