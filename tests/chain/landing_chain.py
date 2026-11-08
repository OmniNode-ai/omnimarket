# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Support for the landing golden, error and whole-flow chain tests (OMN-22922).

Rebuild piece 9 (landing and merge queue) carries a generated golden and error chain
event test and one whole-flow run, each able to fail for real.

* The orchestrator chains are generated from the landing orchestrator's own contract:
  the state machine in ``contract.yaml`` gives the trigger path to the terminal
  (golden) and the trigger path to the agent-needed state (error); a driver table maps
  each trigger to the event that fires it. A contract change that moves a path moves
  the test.
* The decision chains drive the landing decision node (``decide_landing``) tick by
  tick against the fake world of the review scenarios. Every tick's snapshot travels
  to the node over a bus and its decision travels back, so the chain is the one the
  controller will run when the decision moves onto the runtime. The bus is chosen by
  an overlay: in memory by default, a Redpanda broker when the overlay names one.
* A named mutation patches one function of the decision node. A chain test that stays
  green under its mutation cannot fail, so each suite runs its mutation and demands red.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, AsyncExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_node,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingOutcomeReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    TICK_SECONDS,
    LandingWorld,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ORCHESTRATOR_CONTRACT: Final = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_orchestrator"
    / "contract.yaml"
)
DECISION_CONTRACT: Final = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_decision_compute"
    / "contract.yaml"
)
SCENARIOS: Final = (
    REPO_ROOT / "tests" / "fixtures" / "pr_landing_decision" / "review_scenarios"
)

BOOTSTRAP_ENV: Final = "LANDING_CHAIN_BOOTSTRAP"
LANE_ENV: Final = "LANDING_CHAIN_LANE"
ROOT_ENV: Final = "LANDING_CHAIN_REGISTRY_ROOT"
MUTATE_ENV: Final = "LANDING_CHAIN_MUTATE"
RECEIPT_DIR_ENV: Final = "LANDING_CHAIN_RECEIPT_DIR"


def decision_topics() -> dict[str, str]:
    """The decision node's command, success and failure topics, read from its contract."""
    dispatch = yaml.safe_load(DECISION_CONTRACT.read_text(encoding="utf-8"))[
        "runtime_dispatch"
    ]
    return {
        "command": dispatch["command_topic"],
        "success": dispatch["terminal_events"]["success"],
        "failure": dispatch["terminal_events"]["failure"],
    }


class ModelLandingChainOverlay(BaseModel):
    """Every setting of one chain run; the in-memory profile is the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: Literal["inmemory", "redpanda"] = "inmemory"
    lane: str = ""
    registry_root: str = ""
    bootstrap: str = ""
    call_timeout_s: float = Field(default=20.0, gt=0)
    consumer_join_s: float = Field(default=5.0, ge=0)
    receipt_dir: str = ""

    @classmethod
    def for_profile(cls, profile: str) -> ModelLandingChainOverlay:
        receipt_dir = os.environ.get(RECEIPT_DIR_ENV, "")
        if profile == "redpanda":
            return cls(
                profile="redpanda",
                lane=os.environ.get(LANE_ENV, ""),
                registry_root=os.environ.get(ROOT_ENV, ""),
                bootstrap=os.environ.get(BOOTSTRAP_ENV, ""),
                receipt_dir=receipt_dir,
            )
        return cls(receipt_dir=receipt_dir)


# ---------------------------------------------------------------- the bus legs
class DecisionNodeFailedError(AssertionError):
    """The decision node answered with its own failure instead of a decision."""


class DecisionBus:
    """The decision node served over a bus: facts go in, a decision comes back."""

    def __init__(self, overlay: ModelLandingChainOverlay) -> None:
        self.overlay = overlay
        self.run_id = uuid.uuid4().hex
        # The node's own declared topics, a run told apart by its id: a drill never
        # mints a topic.
        topics = decision_topics()
        self.topic_facts = topics["command"]
        self.topic_decision = topics["success"]
        self.topic_failure = topics["failure"]
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.carried: list[str] = []  # topic of every message the bus carried

    async def start(self) -> None:
        raise NotImplementedError

    async def _send(self, topic: str, body: bytes) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    @staticmethod
    def serve(raw: bytes) -> bytes:
        """The decision node's side: read facts, return the decision it computes."""
        request = json.loads(raw)
        try:
            facts = ModelLandingFacts.model_validate(request["facts"])
            decision = decision_node.decide_landing(facts)
        except Exception as exc:
            return json.dumps(
                {
                    "id": request["id"],
                    "run_id": request.get("run_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ).encode()
        return json.dumps(
            {
                "id": request["id"],
                "run_id": request.get("run_id"),
                "decision": decision.model_dump(mode="json"),
            }
        ).encode()

    def _answered(self, raw: bytes) -> None:
        reply = json.loads(raw)
        future = self.waiting.pop(reply["id"], None)
        if future is not None and not future.done():
            future.set_result(reply)

    async def decide(self, facts: ModelLandingFacts) -> ModelLandingDecision:
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[call_id] = future
        body = json.dumps(
            {
                "id": call_id,
                "run_id": self.run_id,
                "facts": facts.model_dump(mode="json"),
            }
        ).encode()
        await self._send(self.topic_facts, body)
        reply = await asyncio.wait_for(future, self.overlay.call_timeout_s)
        if "error" in reply:
            raise DecisionNodeFailedError(reply["error"])
        return ModelLandingDecision.model_validate(reply["decision"])


class InmemoryDecisionBus(DecisionBus):
    def __init__(self, overlay: ModelLandingChainOverlay) -> None:
        super().__init__(overlay)
        self.bus = EventBusInmemory(environment="landing-chain", group="landing-chain")

    async def start(self) -> None:
        await self.bus.start()

        async def on_facts(message: Any) -> None:
            await self._send(self.topic_decision, self.serve(message.value))

        async def on_decision(message: Any) -> None:
            self._answered(message.value)

        await self.bus.subscribe(
            self.topic_facts, on_message=on_facts, group_id="decision-node"
        )
        await self.bus.subscribe(
            self.topic_decision, on_message=on_decision, group_id="controller"
        )

    async def _send(self, topic: str, body: bytes) -> None:
        self.carried.append(topic)
        await self.bus.publish(topic, None, body)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaDecisionBus(DecisionBus):
    """The same two topics on a real broker, opened the way ``onex delegate`` opens it."""

    def __init__(self, overlay: ModelLandingChainOverlay) -> None:
        super().__init__(overlay)
        self.stack = AsyncExitStack()
        self.kafka: Any = None

    async def start(self) -> None:
        from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus

        # The bus opener names the registry-root parameter; read it from its signature.
        root_parameter = next(
            name
            for name in inspect.signature(open_lab_run_bus).parameters
            if name.endswith("_home")
        )
        registry_root = (
            Path(self.overlay.registry_root) if self.overlay.registry_root else None
        )

        self.kafka = await self.stack.enter_async_context(
            open_lab_run_bus(
                bus="kafka",
                lane=self.overlay.lane or None,
                kafka_bootstrap=self.overlay.bootstrap or None,
                **{root_parameter: registry_root},
            )
        )

        async def on_facts(message: Any) -> None:
            if json.loads(message.value).get("run_id") == self.run_id:
                await self._send(self.topic_decision, self.serve(message.value))

        async def on_decision(message: Any) -> None:
            if json.loads(message.value).get("run_id") == self.run_id:
                self._answered(message.value)

        await self.kafka.subscribe(
            self.topic_facts,
            on_message=on_facts,
            group_id=f"landing-chain-node-{self.run_id}",
        )
        await self.kafka.subscribe(
            self.topic_decision,
            on_message=on_decision,
            group_id=f"landing-chain-controller-{self.run_id}",
        )
        await self.kafka.start_consuming()
        await asyncio.sleep(self.overlay.consumer_join_s)

    async def _send(self, topic: str, body: bytes) -> None:
        self.carried.append(topic)
        await self.kafka.publish(topic=topic, key=self.run_id.encode(), value=body)

    async def stop(self) -> None:
        await self.stack.aclose()


def make_bus(overlay: ModelLandingChainOverlay) -> DecisionBus:
    if overlay.profile == "redpanda":
        return RedpandaDecisionBus(overlay)
    return InmemoryDecisionBus(overlay)


# ------------------------------------------------------------- the world chain
class TickRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: int
    actions: tuple[str, ...]
    recorded: tuple[str, ...]
    violations: int


async def run_world_chain(
    scenario: str, overlay: ModelLandingChainOverlay
) -> tuple[LandingWorld, DecisionBus, list[TickRecord]]:
    """Replay one scenario fixture with the decision taken over the overlay's bus.

    Each tick's expectations (actions, recorded outcomes, violations) and the model's
    safety properties are checked the way ``run_scenario`` checks them; the end state
    is checked last.
    """
    spec = yaml.safe_load((SCENARIOS / f"{scenario}.yaml").read_text(encoding="utf-8"))
    world = LandingWorld(spec)
    bus = make_bus(overlay)
    await bus.start()
    records: list[TickRecord] = []
    try:
        for index, tick_spec in enumerate(spec["ticks"], start=1):
            world.tick += 1
            world.now += timedelta(seconds=TICK_SECONDS)
            world.crash = None
            for event in tick_spec.get("events", []):
                world.apply_event(event)
            facts = world.facts()
            world.facts_seen.append(facts)
            world.pending_acks = []
            world.pending_failures = set()
            decision = await bus.decide(facts)
            world._check_properties(facts, decision)
            world.decisions.append(decision)
            for result in facts.results:
                world.workers[result.lease_id].result = None
            world.state = decision.next_state
            live = {lease.lease_id for lease in world.state.leases}
            for worker in world.workers.values():
                if worker.lease_id not in live and any(
                    lease.lease_id == worker.lease_id for lease in facts.state.leases
                ):
                    worker.released = True
            for action in decision.actions:
                world._perform(action)
            world.check(
                decision, tick_spec.get("expect", {}), f"{scenario} tick {index}"
            )
            records.append(
                TickRecord(
                    tick=index,
                    actions=tuple(world.label(a) for a in decision.actions),
                    recorded=tuple(
                        f"{r.pr} {r.outcome.value} {r.reason.value}"
                        for r in decision.recorded_outcomes
                    ),
                    violations=len(decision.violations),
                )
            )
        world.check_end(spec.get("end", {}))
    finally:
        await bus.stop()
    return world, bus, records


# ---------------------------------------------------------- named mutations
@contextmanager
def _patched(name: str, replacement: Callable[..., Any]) -> Iterator[None]:
    original = getattr(decision_node, name)
    setattr(decision_node, name, replacement)
    try:
        yield
    finally:
        setattr(decision_node, name, original)


def mutation_merge_never_lands() -> AbstractContextManager[None]:
    """The decision node computes a merge and drops it."""

    def drop(*_args: object) -> None:
        return None

    return _patched("_merge", drop)


def mutation_invalid_fix_accepted() -> AbstractContextManager[None]:
    """The decision node accepts every fix_submitted result as verified."""

    def accept(*_args: object) -> object:
        return EnumLandingOutcomeReason.NONE

    return _patched("_verify_fix", accept)


def mutation_red_counts_as_mergeable() -> AbstractContextManager[None]:
    """The decision node treats a red pull request as green and clean."""

    def always(*_args: object) -> bool:
        return True

    return _patched("_mergeable", always)


MUTATIONS: Final[dict[str, Callable[[], Any]]] = {
    "merge_never_lands": mutation_merge_never_lands,
    "invalid_fix_accepted": mutation_invalid_fix_accepted,
    "red_counts_as_mergeable": mutation_red_counts_as_mergeable,
}


# ------------------------------------------------- orchestrator chains by contract
def contract_paths() -> dict[str, list[str]]:
    """Shortest trigger paths the orchestrator contract declares.

    ``golden`` ends in ``MERGED`` (the success terminal) having passed through ``ARMED``. One ``error:<trigger>`` path
    per distinct trigger that enters ``NEEDS_AGENT`` from another state ends in the
    agent-needed state (the typed failure a person or agent must take).
    """
    machine = yaml.safe_load(ORCHESTRATOR_CONTRACT.read_text(encoding="utf-8"))[
        "state_machine"
    ]
    initial = machine["initial_state"]
    edges: dict[str, list[tuple[str, str]]] = {}
    for t in machine["transitions"]:
        edges.setdefault(t["from_state"], []).append((t["trigger"], t["to_state"]))

    def shortest(goal: str, via: str | None = None) -> list[str] | None:
        queue: deque[tuple[str, list[str]]] = deque([(initial, [])])
        seen = {initial}
        while queue:
            state, path = queue.popleft()
            for trigger, nxt in edges.get(state, []):
                if via is not None and nxt == goal and trigger != via:
                    continue
                if nxt == goal and state != goal:
                    return [*path, trigger]
                if nxt not in seen and nxt not in machine["terminal_states"]:
                    seen.add(nxt)
                    queue.append((nxt, [*path, trigger]))
        return None

    # A pull request enters the machine by the self-loop trigger on the initial state
    # (the push that opened the episode); every path starts with it.
    entry = next(
        t["trigger"]
        for t in machine["transitions"]
        if t["from_state"] == initial and t["to_state"] == initial
    )
    paths: dict[str, list[str]] = {}
    # The success chain is the one that lands through arming: the shortest path to ARMED,
    # then the trigger that takes ARMED to the merged terminal.
    armed = shortest("ARMED")
    assert armed is not None, "the orchestrator contract has no path to ARMED"
    landing = next(
        t["trigger"]
        for t in machine["transitions"]
        if t["from_state"] == "ARMED" and t["to_state"] == "MERGED"
    )
    paths["golden"] = [entry, *armed, landing]
    entering = sorted(
        {
            t["trigger"]
            for t in machine["transitions"]
            if t["to_state"] == "NEEDS_AGENT" and t["from_state"] != "NEEDS_AGENT"
        }
    )
    for trigger in entering:
        path = shortest("NEEDS_AGENT", via=trigger)
        if path is not None:
            paths[f"error:{trigger}"] = [entry, *path]
    return paths


# ---------------------------------------------------------------- the receipt
def write_receipt(
    overlay: ModelLandingChainOverlay,
    fallback_dir: Path,
    *,
    scenario: str,
    world: LandingWorld,
    bus: DecisionBus,
    records: list[TickRecord],
) -> Path:
    """Record one whole-flow run: where it ran, what the node decided, how it ended."""
    pr_states = {pr: p.state.value for pr, p in world.prs.items()}
    receipt = {
        "ticket": "OMN-22922",
        "scenario": scenario,
        "profile": overlay.profile,
        "bus_run_id": bus.run_id,
        "messages_carried": len(bus.carried),
        "topics": sorted(set(bus.carried)),
        "first_tick_observed_at": world.facts_seen[0].observed_at.isoformat(),
        "ticks": [r.model_dump(mode="json") for r in records],
        "final_pr_states": pr_states,
        "merged": sorted(pr for pr, state in pr_states.items() if state == "merged"),
    }
    directory = Path(overlay.receipt_dir) if overlay.receipt_dir else fallback_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"landing-whole-flow-{overlay.profile}-{bus.run_id}.json"
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path
