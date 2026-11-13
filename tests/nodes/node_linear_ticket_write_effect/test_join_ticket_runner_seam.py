# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test for the seam between the remote-lane runner and the ticket node (OMN-23410).

A lab lane mints the child ticket its pull request needs with ``onex ticket create`` (the
runner side, ``ticket_cli``); the ticket node (``node_linear_ticket_write_effect``) answers on
its completed or failed terminal. The two sides import the same models, so a rename in a model
moves both and no unit test on one side sees it. This test pins the wire the other side reads:
the request keys the runner publishes, the terminal keys the node publishes, and the paths in
the terminal that the lane reads (the minted ticket id, the refusal reason, the guard's finding
codes). A field renamed on either side fails here.

Profiles (``MODEL_TICKET_SEAM_OVERLAY``): the in-memory profile serves the node through the real
runtime dispatch built from its contract, over an in-memory broker. The redpanda profile sends
the same requests over the real broker to the node the lab runtime serves, so it needs a
declared lane and a granted principal; it is skipped when none is named.

The failing controls plant a named rename on one side and require the golden or the error chain
to fail. ``TICKET_SEAM_MUTATE=<name>`` runs the whole file under one mutation to read it fail.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
from unittest import mock
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map
from pydantic import BaseModel, ConfigDict, create_model

from omnimarket.events.ticket_write_io import (
    ModelTicketWriteCompleted,
    ModelTicketWriteFailed,
)
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.ticket import ticket_cli
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardRefusedError,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
    HandlerLinearTicketWriteEffect,
)

pytestmark = pytest.mark.unit

_CONTRACT: Final = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_linear_ticket_write_effect/contract.yaml"
)
LANE_ENV: Final = "TICKET_SEAM_LANE"
BOOTSTRAP_ENV: Final = "TICKET_SEAM_BOOTSTRAP"
ROOT_ENV: Final = "TICKET_SEAM_REGISTRY_ROOT"
MUTATE_ENV: Final = "TICKET_SEAM_MUTATE"

# ---------------------------------------------------------------- the pinned wire
# Written out here, not read from the models: a model that moves must fail this test.
REQUEST_KEYS: Final = frozenset(
    {
        "operation",
        "title",
        "description",
        "ticket_id",
        "body",
        "state",
        "repo",
        "parent",
        "blocked_by",
        "dry_run",
        "team",
        "allow_arch_violation",
        "pillar",
        "day",
        "from_project",
        "project",
        "correlation_id",
        "idempotency_key",
        "requested_by_lane",
        "requested_by_run",
    }
)
IDENTITY_KEYS: Final = frozenset(
    {
        "correlation_id",
        "operation",
        "idempotency_key",
        "requested_by_lane",
        "requested_by_run",
    }
)
COMPLETED_KEYS: Final = IDENTITY_KEYS | {"outcome", "replayed", "result"}
FAILED_KEYS: Final = IDENTITY_KEYS | {
    "outcome",
    "reason",
    "terminal_failure_cause",
    "finding_codes",
}
# What a lane reads from a completed terminal's result: the child ticket it minted.
RESULT_READ_KEYS: Final = frozenset({"status", "ticket_id", "ticket_url", "title"})
LANE: Final = "lane-seam"
RUN: Final = "run-seam"
PARENT: Final = "OMN-23410"
GATE_FINDING: Final = "missing_gate_line"
GOOD_DESCRIPTION: Final = (
    f"created-by-lane: {LANE}\n\nGate: OmniNode-ai/omnimarket ci.yml\n\n"
    "## Acceptance criteria\n\n- [ ] AC1: one -- falsifier: a test\n"
)
MINTED_ID: Final = "OMN-90001"


class ModelTicketSeamOverlay(BaseModel):
    """Every setting of one seam run; the in-memory profile is the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: Literal["inmemory", "redpanda"] = "inmemory"
    lane: str = ""
    bootstrap: str = ""
    registry_root: str = ""
    timeout_s: float = 20.0

    @classmethod
    def for_profile(cls, profile: str) -> ModelTicketSeamOverlay:
        if profile == "redpanda":
            return cls(
                profile="redpanda",
                lane=os.environ.get(LANE_ENV, ""),
                bootstrap=os.environ.get(BOOTSTRAP_ENV, ""),
                registry_root=os.environ.get(ROOT_ENV, ""),
            )
        return cls()

    def bus_args(self) -> list[str]:
        if self.profile == "inmemory":
            return []
        args = ["--bus", "kafka"]
        if self.lane:
            args += ["--bus-lane", self.lane]
        if self.bootstrap:
            args += ["--kafka-bootstrap", self.bootstrap]
        if self.registry_root:
            args += ["--omnibase-path", self.registry_root]
        return args


@pytest.fixture(
    params=[
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ]
)
def overlay(request: pytest.FixtureRequest) -> ModelTicketSeamOverlay:
    chosen = ModelTicketSeamOverlay.for_profile(request.param)
    if chosen.profile == "redpanda" and not (chosen.bootstrap or chosen.lane):
        pytest.skip(f"no broker: {LANE_ENV} and {BOOTSTRAP_ENV} name none")
    return chosen


# ---------------------------------------------------------------- the node side
class GateCore:
    """The node's create core, with the gate's one rule this test needs.

    The real guard is the omniclaude hook; the redpanda profile and the lab drill reach it. Here
    a create without a ``Gate:`` line is refused with the guard's own finding code, which is all
    the seam reads.
    """

    def __init__(self) -> None:
        self.seen: list[ModelCreateTicketRequest] = []

    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult:
        self.seen.append(request)
        if request.operation is EnumTicketOperation.CREATE and (
            "Gate:" not in request.description
        ):
            raise TicketGuardRefusedError("refused", [GATE_FINDING])
        return ModelCreateTicketResult(
            operation=request.operation,
            status="created",
            ticket_id=MINTED_ID,
            ticket_url=f"https://linear.example.invalid/{MINTED_ID}",
            title=request.title,
        )


@dataclass
class Message:
    value: bytes
    partition: int = 0
    offset: str = "0"


class RuntimeTicketBus:
    """The runner's bus handle, backed by the node served through its real runtime dispatch."""

    def __init__(self, core: GateCore) -> None:
        manifest = discover_contracts_from_paths([_CONTRACT])
        assert not manifest.errors
        (contract,) = manifest.contracts
        self.topics = ticket_cli.load_ticket_topics()
        handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _lane: core)
        routing = build_routing_map(
            [contract],
            frozenset([self.topics.command]),
            handler_resolver=lambda _name: handler,
        )
        self.broker = InMemoryBroker(num_partitions=1)
        self.transport = InMemoryTransport(
            broker=self.broker, group="ticket-seam", topics=[self.topics.command]
        )
        self.dispatch = RuntimeDispatch(
            consumer=self.transport,
            producer=self.transport,
            routing_map=routing,
            dlq_topic_resolver=derive_canonical_dlq_topic,
        )
        self.callbacks: dict[str, Callable[[Message], Any]] = {}
        self.published: list[dict[str, Any]] = []

    async def subscribe(
        self,
        topic: str,
        node_identity: object = None,
        on_message: object = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Any]:
        # A ticket call reads its terminals with no consumer group (OMN-21818).
        raise AssertionError(f"the runner joined a consumer group on {topic}")

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Message], Any]
    ) -> Callable[[], Any]:
        self.callbacks[topic] = on_message

        async def unsubscribe() -> None:
            del self.callbacks[topic]

        return unsubscribe

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, bytes] | None = None,
    ) -> None:
        assert topic == self.topics.command
        envelope = ModelEventEnvelope[dict[str, Any]].model_validate_json(value)
        self.published.append(envelope.payload)
        await self.transport.send(topic, key=key or b"ticket", value=value, headers={})
        await self.dispatch.drain()
        for terminal in (self.topics.success, self.topics.failure):
            for record in self.broker.records(terminal, 0):
                if terminal in self.callbacks:
                    await self.callbacks[terminal](Message(value=record.value))


# ---------------------------------------------------------------- the runner
@dataclass
class Run:
    exit_code: int
    answer: dict[str, Any]
    published: list[dict[str, Any]]
    core: GateCore


def mint(
    overlay: ModelTicketSeamOverlay, monkeypatch: pytest.MonkeyPatch, **fields: str
) -> Run:
    """Run ``onex ticket create`` as a lane runs it and read what it prints."""
    core = GateCore()
    bus = RuntimeTicketBus(core)
    if overlay.profile == "inmemory":

        @contextlib.asynccontextmanager
        async def opened(_opts: object) -> Any:
            yield bus

        monkeypatch.setattr(ticket_cli, "_open_bus", opened)
    args = [
        "create",
        "--title",
        fields.get("title", "Scratch child"),
        "--description",
        fields.get("description", GOOD_DESCRIPTION),
        "--parent",
        fields.get("parent", PARENT),
        "--lane",
        LANE,
        "--run",
        RUN,
        "--timeout-s",
        str(overlay.timeout_s),
        *overlay.bus_args(),
    ]
    if overlay.profile == "redpanda":
        # The live node runs its guard and stops before Linear: the drill mints nothing.
        args.append("--dry-run")
    result = CliRunner().invoke(ticket_cli.ticket_group, args)
    assert result.output, repr(result.exception)
    # The broker profile's client logs to the same stream; the answer is the last JSON line.
    lines = [ln for ln in result.output.splitlines() if ln.startswith("{")]
    assert lines, result.output
    return Run(result.exit_code, json.loads(lines[-1]), bus.published, core)


def read_lane_view(answer: dict[str, Any]) -> dict[str, Any]:
    """What a lane reads from the printed terminal, by the paths it reads them at."""
    payload = answer["payload"]
    if answer["failed"]:
        return {
            "reason": payload["reason"],
            "finding_codes": payload["finding_codes"],
        }
    result = payload["result"]
    return {key: result[key] for key in RESULT_READ_KEYS}


# ---------------------------------------------------------------- failing controls
def _rename(data: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    renamed = dict(data)
    renamed[new] = renamed.pop(old)
    return renamed


@contextlib.contextmanager
def runner_renames_lane() -> Iterator[None]:
    """The runner publishes ``lane`` where the node reads ``requested_by_lane``."""
    real = ScmBusCaller.call_topics

    async def call_topics(self: ScmBusCaller, **kw: Any) -> Any:
        request = kw["request"]

        class Renamed:
            def model_dump(self, **options: Any) -> dict[str, Any]:
                return _rename(
                    request.model_dump(**options), "requested_by_lane", "lane"
                )

        return await real(self, **{**kw, "request": Renamed()})

    with mock.patch.object(ScmBusCaller, "call_topics", call_topics):
        yield


def _renamed_output(old: str, new: str) -> AbstractContextManager[None]:
    """The node publishes ``new`` where the runner reads ``old``."""
    real = HandlerLinearTicketWriteEffect.handle

    async def handle(self: HandlerLinearTicketWriteEffect, request: Any) -> Any:
        result = await real(self, request)
        data = result.model_dump(mode="json")
        if old not in data:
            return result
        fields: dict[str, Any] = {
            k: (Any, v) for k, v in _rename(data, old, new).items()
        }
        make: Any = create_model
        return make(
            type(result).__name__,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )()

    return mock.patch.object(HandlerLinearTicketWriteEffect, "handle", handle)


MUTATIONS: Final[dict[str, Callable[[], AbstractContextManager[None]]]] = {
    "runner-renames-lane": runner_renames_lane,
    "node-renames-reason": lambda: _renamed_output("reason", "why"),
    "node-renames-result": lambda: _renamed_output("result", "outcome_result"),
}


@pytest.fixture(autouse=True)
def injected_break() -> Iterator[None]:
    """Run the whole file under one named mutation, to read it fail for real."""
    name = os.environ.get(MUTATE_ENV)
    if not name:
        yield
        return
    with MUTATIONS[name]():
        yield


# ---------------------------------------------------------------- the chains
def assert_golden(run: Run, overlay: ModelTicketSeamOverlay) -> None:
    assert run.exit_code == 0, run.answer
    assert not run.answer["failed"]
    payload = run.answer["payload"]
    assert set(payload) == COMPLETED_KEYS
    assert payload["outcome"] == "completed"
    assert payload["requested_by_lane"] == LANE
    assert payload["requested_by_run"] == RUN
    # The node's own model reads what the node published, and the lane reads its paths.
    assert ModelTicketWriteCompleted.model_validate(payload).outcome == "completed"
    view = read_lane_view(run.answer)
    if overlay.profile == "inmemory":
        assert view["ticket_id"] == MINTED_ID
        assert view["status"] == "created"
        assert run.core.seen[0].parent == PARENT
        assert run.core.seen[0].description == GOOD_DESCRIPTION
    else:
        assert view["status"] == "dry_run"


def assert_error(run: Run) -> None:
    assert run.exit_code == 1, run.answer
    assert run.answer["failed"]
    payload = run.answer["payload"]
    assert set(payload) == FAILED_KEYS
    assert ModelTicketWriteFailed.model_validate(payload).reason == "guard_refused"
    view = read_lane_view(run.answer)
    assert view["reason"] == "guard_refused"
    assert GATE_FINDING in view["finding_codes"]
    assert payload["requested_by_lane"] == LANE


def test_golden_a_lane_mints_a_child_ticket_through_the_node(
    overlay: ModelTicketSeamOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = mint(overlay, monkeypatch)
    assert_golden(run, overlay)
    if overlay.profile == "inmemory":
        assert [set(p) for p in run.published] == [REQUEST_KEYS]
        assert UUID(run.published[0]["correlation_id"])


def test_error_a_malformed_request_is_refused_with_the_gates_own_reason(
    overlay: ModelTicketSeamOverlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = mint(
        overlay,
        monkeypatch,
        description=f"created-by-lane: {LANE}\n\nno gate line, no criteria\n",
    )
    assert_error(run)
    if overlay.profile == "inmemory":
        assert [c.parent for c in run.core.seen] == [PARENT]


def test_the_pinned_wire_is_what_the_models_declare() -> None:
    """A field added to or renamed in a model must be a decision made here, on both sides."""
    from omnimarket.events.ticket_write_io import (
        ModelTicketWriteRequest,
    )

    assert set(ModelTicketWriteRequest.model_fields) == REQUEST_KEYS
    assert set(ModelTicketWriteCompleted.model_fields) == COMPLETED_KEYS
    assert set(ModelTicketWriteFailed.model_fields) == FAILED_KEYS
    assert set(ModelCreateTicketResult.model_fields) >= RESULT_READ_KEYS


def test_the_contract_topics_are_the_ones_the_runner_calls() -> None:
    dispatch = yaml.safe_load(_CONTRACT.read_text())["runtime_dispatch"]
    topics = ticket_cli.load_ticket_topics()
    assert topics.command == dispatch["command_topic"]
    assert topics.success == dispatch["terminal_events"]["success"]
    assert topics.failure == dispatch["terminal_events"]["failure"]


def run_both_chains(overlay: ModelTicketSeamOverlay, mp: pytest.MonkeyPatch) -> None:
    assert_golden(mint(overlay, mp), overlay)
    assert_error(
        mint(overlay, mp, description=f"created-by-lane: {LANE}\n\nno gate line\n")
    )


@pytest.mark.parametrize(
    "mutation", ["runner-renames-lane", "node-renames-reason", "node-renames-result"]
)
def test_control_the_seam_is_red_under_a_planted_rename(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = ModelTicketSeamOverlay()
    with MUTATIONS[mutation](), pytest.raises((AssertionError, KeyError, ValueError)):
        run_both_chains(overlay, monkeypatch)
