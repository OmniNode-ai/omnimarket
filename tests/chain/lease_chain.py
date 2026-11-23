# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Support for the lease receipt golden and error chain tests (OMN-24484).

The lease receipt reducer is definition-B compute: one command in, one reply
out, the lease table owned by the caller. The chain tests drive the real node
over a bus the way the landing chains drive the decision node: the caller
sends a reduce command on the node's own command topic, the node side
validates it and runs ``HandlerLeaseReceiptReducer().handle``, and the reply
travels back on the terminal the contract names — the recorded terminal for a
recorded reply, the refused terminal for a refused one. The bus is chosen by
an overlay: in memory by default, a Redpanda broker when the overlay names
one.

A named mutation patches one rule function of the reducer. A chain test that
stays green under its mutation cannot fail, so each suite runs its mutation
and demands red.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, AsyncExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseReason,
    EnumLeaseReleaseRule,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_overlay import (
    ModelLeaseKindConfig,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers import (
    handler_lease_receipt_reducer as lease_node,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
LEASE_CONTRACT: Final = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lease_receipt_reducer"
    / "contract.yaml"
)

BOOTSTRAP_ENV: Final = "LEASE_CHAIN_BOOTSTRAP"
LANE_ENV: Final = "LEASE_CHAIN_LANE"
ROOT_ENV: Final = "LEASE_CHAIN_REGISTRY_ROOT"
RECEIPT_DIR_ENV: Final = "LEASE_CHAIN_RECEIPT_DIR"

# The packaged default overlay's one kind and its admitted caller; the tests
# read them here so a contract change that renames either moves the tests.
LANDING_KIND: Final = "landing"
LANDING_CALLER: Final = "landing-controller"


def lease_topics() -> dict[str, str]:
    """The lease node's command, recorded and refused topics, read from its contract."""
    dispatch = yaml.safe_load(LEASE_CONTRACT.read_text(encoding="utf-8"))[
        "runtime_dispatch"
    ]
    return {
        "command": dispatch["command_topic"],
        "recorded": dispatch["terminal_events"]["success"],
        "refused": dispatch["terminal_events"]["failure"],
    }


class ModelLeaseChainOverlay(BaseModel):
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
    def for_profile(cls, profile: str) -> ModelLeaseChainOverlay:
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
class LeaseNodeFailedError(AssertionError):
    """The lease node answered with its own failure instead of a reduce reply."""


def make_command(
    operation: Any,
    table: ModelLeaseTable,
    subject: str,
    now: datetime,
    correlation_id: str,
    *,
    principal: str = LANDING_CALLER,
    channel: Literal["in_process", "bus"] = "bus",
    **fields: Any,
) -> ModelLeaseReduceInput:
    """One reduce command from the chain's caller, in the chain caller's words."""
    return ModelLeaseReduceInput(
        correlation_id=correlation_id,
        caller=ModelLeaseCaller(principal=principal, channel=channel),
        operation=operation,
        table=table,
        subject=subject,
        now=now,
        **fields,
    )


class LeaseBus:
    """The lease receipt reducer served over a bus: a command goes in, a reply comes back."""

    def __init__(self, overlay: ModelLeaseChainOverlay) -> None:
        self.overlay = overlay
        self.run_id = uuid.uuid4().hex
        # The node's own declared topics, a run told apart by its id: a drill never
        # mints a topic.
        topics = lease_topics()
        self.topic_command = topics["command"]
        self.topic_recorded = topics["recorded"]
        self.topic_refused = topics["refused"]
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.carried: list[str] = []  # topic of every message the bus carried
        # The node's one long-lived handler: it owns the lease tables (single writer), so every
        # command of a run meets the table the run's earlier commands wrote. The chain drives
        # virtual time through its commands, so the node's clock reads the command's own now;
        # the clock bound itself is read in the reducer's now-bounds tests.
        self.node_now = datetime.now(UTC)
        self.node = HandlerLeaseReceiptReducer(clock=lambda: self.node_now)

    async def start(self) -> None:
        raise NotImplementedError

    async def _send(self, topic: str, body: bytes) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    def serve(self, raw: bytes) -> bytes:
        """The node's side: validate the command, reduce it, return the reply."""
        request = json.loads(raw)
        try:
            command = ModelLeaseReduceInput.model_validate(request["command"])
            self.node_now = command.now
            reply = self.node.handle(command)
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
                "reply": reply.model_dump(mode="json"),
            }
        ).encode()

    def reply_topic(self, raw: bytes) -> str:
        """A recorded reply goes out on the recorded terminal, a refused one on refused."""
        body = json.loads(raw)
        if "error" in body:
            return self.topic_refused
        recorded = body["reply"]["status"] == EnumLeaseStatus.RECORDED.value
        return self.topic_recorded if recorded else self.topic_refused

    def _answered(self, raw: bytes) -> None:
        reply = json.loads(raw)
        future = self.waiting.pop(reply["id"], None)
        if future is not None and not future.done():
            future.set_result(reply)

    async def call(self, request: ModelLeaseReduceInput) -> ModelLeaseReduceOutput:
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[call_id] = future
        body = json.dumps(
            {
                "id": call_id,
                "run_id": self.run_id,
                "command": request.model_dump(mode="json"),
            }
        ).encode()
        await self._send(self.topic_command, body)
        reply = await asyncio.wait_for(future, self.overlay.call_timeout_s)
        if "error" in reply:
            raise LeaseNodeFailedError(reply["error"])
        return ModelLeaseReduceOutput.model_validate(reply["reply"])


class InmemoryLeaseBus(LeaseBus):
    def __init__(self, overlay: ModelLeaseChainOverlay) -> None:
        super().__init__(overlay)
        self.bus = EventBusInmemory(environment="lease-chain", group="lease-chain")

    async def start(self) -> None:
        await self.bus.start()

        async def on_command(message: Any) -> None:
            reply = self.serve(message.value)
            await self._send(self.reply_topic(reply), reply)

        async def on_reply(message: Any) -> None:
            self._answered(message.value)

        await self.bus.subscribe(
            self.topic_command, on_message=on_command, group_id="lease-node"
        )
        for terminal in (self.topic_recorded, self.topic_refused):
            await self.bus.subscribe(
                terminal, on_message=on_reply, group_id="lease-controller"
            )

    async def _send(self, topic: str, body: bytes) -> None:
        self.carried.append(topic)
        await self.bus.publish(topic, None, body)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaLeaseBus(LeaseBus):
    """The same topics on a real broker, opened the way ``onex delegate`` opens it."""

    def __init__(self, overlay: ModelLeaseChainOverlay) -> None:
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

        async def on_command(message: Any) -> None:
            if json.loads(message.value).get("run_id") == self.run_id:
                await self._send(self.topic_recorded, self.serve(message.value))

        async def on_reply(message: Any) -> None:
            if json.loads(message.value).get("run_id") == self.run_id:
                self._answered(message.value)

        await self.kafka.subscribe(
            self.topic_command,
            on_message=on_command,
            group_id=f"lease-chain-node-{self.run_id}",
        )
        # The dev principal's grants name the success terminal only (OMN-23395), so the
        # broker-backed bus carries every reply there and never touches the refused
        # topic; the reply body keeps its status, so the controller still reads a
        # refusal as a refusal.
        await self.kafka.subscribe(
            self.topic_recorded,
            on_message=on_reply,
            group_id=f"lease-chain-controller-{self.run_id}",
        )
        await self.kafka.start_consuming()
        await asyncio.sleep(self.overlay.consumer_join_s)

    async def _send(self, topic: str, body: bytes) -> None:
        self.carried.append(topic)
        await self.kafka.publish(topic=topic, key=self.run_id.encode(), value=body)

    async def stop(self) -> None:
        await self.stack.aclose()


def make_lease_bus(overlay: ModelLeaseChainOverlay) -> LeaseBus:
    if overlay.profile == "redpanda":
        return RedpandaLeaseBus(overlay)
    return InmemoryLeaseBus(overlay)


# -------------------------------------------------------------- the receipt
def write_receipt(
    overlay: ModelLeaseChainOverlay,
    fallback_dir: Path,
    *,
    case: str,
    bus: LeaseBus,
    results: list[ModelLeaseReduceOutput],
) -> Path:
    """Record one chain run: where it ran, what the node replied, what crossed the bus."""
    receipt = {
        "ticket": "OMN-24484",
        "case": case,
        "profile": overlay.profile,
        "bus_run_id": bus.run_id,
        "messages_carried": len(bus.carried),
        "topics": sorted(set(bus.carried)),
        "results": [r.model_dump(mode="json") for r in results],
    }
    directory = Path(overlay.receipt_dir) if overlay.receipt_dir else fallback_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"lease-receipt-{case}-{overlay.profile}-{bus.run_id}.json"
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------- named mutations
@contextmanager
def _patched(name: str, replacement: Any) -> Iterator[None]:
    original = getattr(lease_node, name)
    setattr(lease_node, name, replacement)
    try:
        yield
    finally:
        setattr(lease_node, name, original)


def mutation_no_epoch_raise() -> AbstractContextManager[None]:
    """The grant writes the counter it granted from: the epoch is reused, not raised."""

    def reused_epoch(table: ModelLeaseTable) -> int:
        return table.next_epoch

    return _patched("_epoch_after_grant", reused_epoch)


def mutation_revoked_not_refused() -> AbstractContextManager[None]:
    """The table's revoked epochs stop refusing a receipt outright."""

    def nothing_revoked(table: ModelLeaseTable, epoch: int) -> bool:
        return False

    return _patched("_is_revoked_epoch", nothing_revoked)


def mutation_release_on_deadline() -> AbstractContextManager[None]:
    """An evidence kind releases on the deadline without evidence, like an expiry kind."""
    original = lease_node._release_allowed

    def allow(
        kind: ModelLeaseKindConfig,
        lease: ModelLeaseEntry,
        now: datetime,
        evidence: bool,
    ) -> EnumLeaseReason:
        if (
            kind.release_rule is EnumLeaseReleaseRule.EVIDENCE
            and now >= lease.deadline_at
        ):
            return EnumLeaseReason.NONE
        return original(kind, lease, now, evidence)

    return _patched("_release_allowed", allow)


def mutation_equal_epoch_other_holder() -> AbstractContextManager[None]:
    """Any run may present the receipt of an equal epoch, not only the holder's."""

    def any_holder(lease: ModelLeaseEntry, holder_run: str | None) -> bool:
        return True

    return _patched("_holder_matches", any_holder)


MUTATIONS: Final[dict[str, Any]] = {
    "no-epoch-raise": mutation_no_epoch_raise,
    "revoked-not-refused": mutation_revoked_not_refused,
    "release-on-deadline": mutation_release_on_deadline,
    "equal-epoch-other-holder": mutation_equal_epoch_other_holder,
}


# -------------------------------------------------- the profiles, conftest-free
# tests/chain/conftest.py names the landing overlays, so the lease suites take
# their profile and mutation fixtures from here: import them into the test
# module and pytest collects them there.
LEASE_PROFILES: Final[list[Any]] = [
    pytest.param("inmemory", id="inmemory"),
    pytest.param(
        "redpanda", id="redpanda", marks=[pytest.mark.integration, pytest.mark.kafka]
    ),
]


@pytest.fixture(params=LEASE_PROFILES)
def lease_overlay(request: pytest.FixtureRequest) -> ModelLeaseChainOverlay:
    chosen = ModelLeaseChainOverlay.for_profile(request.param)
    if chosen.profile == "redpanda" and not (chosen.bootstrap or chosen.lane):
        pytest.skip(f"no broker: {LANE_ENV} and {BOOTSTRAP_ENV} name none")
    return chosen
