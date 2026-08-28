# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event Chain Gate — real omnimarket chains through the real dispatch seam (OMN-16774).

Why this suite exists.

OMN-16767 killed the entire delegation chain on the .201 dev lane and every
repo's CI stayed green. ``HandlerRoutingIntent`` was handed the projection arm's
raw ``input_data`` dict instead of a validated ``ModelRoutingIntent``, raised
``AttributeError: 'dict' object has no attribute 'payload'`` on its first
dereference, and every routing request went to the platform quarantine sink.

The TRIGGER lived in this repo. ``node_delegation_routing_reducer``'s
``contract.yaml`` gained a ``db_io.db_tables`` block for its tenant-overlay table
(OMN-15631) — a table the handler reads through its OWN resolver, never through
``input_data['_db']`` — and that contract change silently flipped the runtime's
wiring arm underneath an unchanged handler. No code in this repo changed shape;
a YAML block did.

That is exactly why this gate belongs here and not only in omnibase_infra: the
defect class is a CONTRACT edit in omnimarket changing how omnibase_infra wires
the handler. Nothing in this repo's CI drove that seam, so the contract edit
merged green and took delegation down.

What is real here.

Everything. This suite mocks nothing:

* the REAL ``contract.yaml`` on disk, parsed by the REAL
  ``discover_contracts_from_paths``;
* the REAL ``HandlerRoutingIntent``, imported by the REAL resolver from the
  module path the contract names;
* the REAL ``_prepare_handler_wiring`` arm selection;
* the REAL ``MessageDispatchEngine``;
* the REAL ``EventBusInmemory`` — the default local transport, so there is no
  broker and no database anywhere in this job.

The message is published as raw JSON bytes, the shape Kafka delivers. The
assertions are the two the outage needed and nobody was making: the terminal
event actually lands, and NOTHING reaches the quarantine sink.

Adding a chain.

Append a ``ChainCase`` row to ``CHAIN_CASES`` naming the node directory, entry
topic, terminal topic and wire payload. A chain whose dispatch seam belongs to
omnibase_infra is gated by that repo's half of this suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.contracts.subcontracts.model_event_bus_subcontract import (
    ModelEventBusSubcontract,
)
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_core.models.primitives.model_semver import ModelSemVer
from omnibase_core.protocols.event_bus.protocol_event_bus_subscriber import (
    ProtocolEventBusSubscriber,
)
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver
from omnibase_core.services.service_local_handler_ownership_query import (
    ServiceLocalHandlerOwnershipQuery,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    PreparedWiring,
    _prepare_handler_wiring,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    EventBusSubcontractWiring,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.service_dispatch_result_applier import DispatchResultApplier
from omnibase_infra.topology import load_topology_profile
from omnibase_spi.protocols.runtime import ProtocolDispatchEngine

# NOT tests/integration/: in this repo `integration` means a REAL Kafka bus
# (tests/conftest.py's OMN-8726 guard rejects EventBusInmemory there, and it is
# right to). This gate is deliberately broker-free, so it lives outside that
# tree and is marked `unit` — it needs no service of any kind.
pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# The platform quarantine sink. A chain that dies silently ends up here, which is
# exactly how OMN-16767 hid behind green CI.
QUARANTINE_TOPIC = "onex.dlq.omnibase-infra.quarantine.v1"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"

# The tier `summarization` routes to first: `local-reasoner`
# (src/omnimarket/configs/routing_tiers.yaml, model Qwen3.6-27B-MTP, OMN-15630).
# Its repo-default `endpoint_url` is null — a site-specific local address the
# overlay is required to supply (OMN-12815/OMN-15807) — so with no overlay the
# routing authority reports NO configured endpoint for this task type and the
# reducer raises before it can emit a decision.
_LOCAL_TIER_BACKEND_ID = "local-reasoner"

# The discard port. Syntactically a COMPLETE chat-completions URL (a bare base is
# rejected, OMN-12815) and deliberately unreachable: the node under gate is a
# routing REDUCER — it decides which backend to use and emits ModelRoutingDecision.
# It never opens a connection, so a live address here would buy nothing and would
# make this gate depend on a running server.
_UNREACHABLE_ENDPOINT_URL = "http://127.0.0.1:9/v1/chat/completions"


@pytest.fixture(autouse=True)
def _hermetic_routing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Make tier routability a fact about the repo, not about the machine.

    This gate ran GREEN on a developer laptop and RED on the CI runner at the
    omnibase-infra 0.38.11 pin (OMN-16815), with::

        ProtocolConfigurationError: [ONEX_CORE_041_INVALID_CONFIGURATION]
        No tier has a configured endpoint for task_type='summarization'.

    Nothing about the chain differed. The laptop's shell carried live cloud
    credentials (``GEMINI_API_KEY``, ``LLM_GLM_API_KEY``, ``OPEN_ROUTER_API_KEY``),
    which made the cheap_cloud rung routable and let routing succeed there; the
    runner has none, and the local rung was unroutable on both because its
    ``endpoint_url`` is null in the repo default. So the gate's verdict was a
    function of the ambient environment — the OMN-16796 defect class, in the
    suite written to catch chains dying silently.

    Binding a minimal overlay makes the LOCAL rung routable everywhere, with no
    credential and no server. ``tests/conftest.py::_ensure_bifrost_contract_path``
    unconditionally clears ``BIFROST_OVERLAY_PATH``; this module-level autouse
    fixture runs after it and therefore wins, which is the documented override
    path.
    """
    overlay = tmp_path / "bifrost_overlay_chain_gate.yaml"
    overlay.write_text(
        "config_version: '2.4.0'\n"
        "schema_version: 'bifrost_delegation.v1'\n"
        "backends:\n"
        f"  - backend_id: {_LOCAL_TIER_BACKEND_ID}\n"
        f"    endpoint_url: '{_UNREACHABLE_ENDPOINT_URL}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))


@dataclass(frozen=True)
class ChainCase:
    """One end-to-end chain driven through the real dispatch seam.

    Attributes:
        chain_id: pytest parameter id; also the consumer-group node name, so a
            failure names the chain.
        node_dir: directory under ``src/omnimarket/nodes`` whose REAL
            ``contract.yaml`` is discovered and wired. Nothing is synthesised.
        entry_topic: the topic the raw wire bytes are published to.
        terminal_topic: the topic the terminal event must land on.
        wire_payload: the envelope's ``payload`` as JSON-safe primitives, exactly
            as it arrives off the wire.
        terminal_type_name: class name of the expected terminal payload.
        broken_by: open ticket that makes this row fail TODAY, or ``""`` when the
            row must pass. Set it and the row is marked ``xfail(strict=True)``:
            it does not block this PR, but the row XPASSes and CI goes RED the
            moment the cited fix lands, forcing the marker's removal. Never use
            this to silence a NEW failure — a chain that starts failing is a dead
            chain, which is the entire thing this gate exists to catch.
    """

    chain_id: str
    node_dir: str
    entry_topic: str
    terminal_topic: str
    wire_payload: dict[str, object]
    terminal_type_name: str
    broken_by: str = ""

    @property
    def contract_path(self) -> Path:
        return _SRC_ROOT / self.node_dir / "contract.yaml"

    def as_param(self) -> object:
        """Wrap this row for ``parametrize``, applying ``broken_by`` if set."""
        if not self.broken_by:
            return pytest.param(self, id=self.chain_id)
        return pytest.param(
            self,
            id=self.chain_id,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    f"{self.broken_by}: chain is dead today. STRICT — this turns "
                    f"RED (XPASS) the moment the fix lands, which is the signal "
                    f"to delete this marker."
                ),
            ),
        )


CHAIN_CASES: tuple[ChainCase, ...] = (
    ChainCase(
        chain_id="delegation-routing-reducer",
        node_dir="node_delegation_routing_reducer",
        entry_topic="onex.cmd.omnibase-infra.delegation-routing-request.v1",
        terminal_topic="onex.evt.omnibase-infra.routing-decision.v1",
        wire_payload={
            "payload": {
                "prompt": "summarize the chain gate",
                # A real EnumTaskType literal — the typed arm VALIDATES this, so
                # an invalid value here would fail the row for a payload reason
                # rather than the defect under gate.
                "task_type": "summarization",
                "correlation_id": "7a300827-0000-4000-8000-000000000001",
                "max_tokens": 2048,
                "emitted_at": "2026-08-27T17:32:00+00:00",
            },
            "min_tier_name": None,
            "excluded_backend_refs": [],
        },
        terminal_type_name="ModelRoutingDecision",
        # LIVE as of the omnibase-infra 0.38.11 pin (OMN-16815). This row was
        # xfail(strict=True) on OMN-16767: the node's contract declares
        # db_io.db_tables (the OMN-15631 tenant-overlay table) while its handler
        # is canonical def-B (`handle(self, intent: ModelRoutingIntent) ->
        # ModelRoutingDecision`), and pre-0.38.11 wiring selected the PROJECTION
        # arm on db_tables alone, handing the typed handler a raw dict. The fix
        # (omnibase_infra#2937, plus #2943/#2949/#2951) first shipped in
        # v0.38.11; the row XPASSed the moment the lock resolved it, which is
        # exactly the signal the marker was written to produce, so the marker is
        # gone. It is now an ordinary passing row: if this chain dies again, this
        # goes RED with no marker to hide behind.
    ),
)


@dataclass
class ChainRun:
    """What one chain execution produced, for assertion by the tests."""

    prepared: PreparedWiring
    terminal_messages: list[bytes]
    quarantine_messages: list[bytes]


async def _run_chain(case: ChainCase) -> ChainRun:
    """Drive one chain end to end from the REAL contract on disk.

    No patching: the resolver imports the real handler class named by the real
    contract, and the real wiring picks the arm.
    """
    assert case.contract_path.exists(), (
        f"[{case.chain_id}] contract not found at {case.contract_path} — the node "
        f"was renamed or removed and this gate row is stale"
    )

    manifest = discover_contracts_from_paths([case.contract_path])
    assert not manifest.errors, (
        f"[{case.chain_id}] the REAL contract failed to parse: {manifest.errors}"
    )
    assert len(manifest.contracts) == 1
    contract = manifest.contracts[0]
    assert contract.handler_routing is not None, (
        f"[{case.chain_id}] contract declares no handler_routing"
    )
    entry = contract.handler_routing.handlers[0]

    bus = EventBusInmemory(environment="chain-gate", group="chain-gate")
    await bus.start()

    terminal_messages: list[bytes] = []
    quarantine_messages: list[bytes] = []

    async def _collect_terminal(message: object) -> None:
        terminal_messages.append(cast("bytes", getattr(message, "value", b"")))

    async def _collect_quarantine(message: object) -> None:
        quarantine_messages.append(cast("bytes", getattr(message, "value", b"")))

    await bus.subscribe(
        case.terminal_topic,
        on_message=_collect_terminal,
        group_id=f"chain-gate-terminal-{case.chain_id}",
    )
    await bus.subscribe(
        QUARANTINE_TOPIC,
        on_message=_collect_quarantine,
        group_id=f"chain-gate-quarantine-{case.chain_id}",
    )

    engine = MessageDispatchEngine()

    # ---- THE SEAM UNDER GATE: real arm selection from the real contract ----
    prepared = _prepare_handler_wiring(
        contract=contract,
        entry=entry,
        dispatch_engine=engine,
        resolver=ServiceHandlerResolver(),
        ownership_query=ServiceLocalHandlerOwnershipQuery(
            local_node_names=frozenset({contract.name})
        ),
        event_bus=bus,
        container=None,
        # The REAL checked-in topology. A contract that declares db_io is
        # refused outright without one, so passing None would make this row
        # fail for a harness reason instead of the defect under gate.
        topology=load_topology_profile("local"),
    )

    engine.register_dispatcher(
        dispatcher_id=prepared.dispatcher_id,
        dispatcher=prepared.dispatcher,
        category=prepared.category,
        message_types=prepared.message_types,
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id=f"{case.chain_id}-route",
            topic_pattern=case.entry_topic,
            message_category=prepared.category,
            # NOTE: the field is ``handler_id``, not ``dispatcher_id``. The model
            # silently drops unknown keys, so a wrong name here does NOT raise —
            # the route just stops binding and the chain matches on
            # topic+category alone.
            handler_id=prepared.dispatcher_id,
        )
    )
    engine.freeze()

    applier = DispatchResultApplier(
        event_bus=cast("ProtocolEventBusLike", bus),
        output_topic=case.terminal_topic,
        output_topic_map={case.terminal_type_name: case.terminal_topic},
        allowed_output_topics=(case.terminal_topic,),
    )

    wiring = EventBusSubcontractWiring(
        event_bus=cast("ProtocolEventBusSubscriber", bus),
        dispatch_engine=cast("ProtocolDispatchEngine", engine),
        environment="chain-gate",
        node_name=case.chain_id,
        service="omnimarket",
        version="v1",
        result_applier=applier,
    )
    await wiring.wire_subscriptions(
        ModelEventBusSubcontract(
            version=ModelSemVer(major=1, minor=0, patch=0),
            subscribe_topics=[case.entry_topic],
            publish_topics=[case.terminal_topic],
        ),
        case.chain_id,
    )

    # ---- The wire message, exactly as Kafka delivers it: raw JSON bytes ----
    await bus.publish(
        case.entry_topic,
        key=None,
        value=json.dumps(
            {
                "payload": case.wire_payload,
                "event_type": case.entry_topic,
                "correlation_id": str(uuid4()),
                "source_tool": "chain-gate",
            }
        ).encode("utf-8"),
    )

    await wiring.cleanup()
    await bus.close()

    return ChainRun(
        prepared=prepared,
        terminal_messages=terminal_messages,
        quarantine_messages=quarantine_messages,
    )


@pytest.mark.parametrize("case", [c.as_param() for c in CHAIN_CASES])
async def test_chain_reaches_terminal_and_never_quarantines(case: ChainCase) -> None:
    """A chain published as raw wire bytes must terminalize and never quarantine.

    This is the assertion pair the OMN-16767 outage needed and nobody was making.
    The chain died into the quarantine sink and produced no terminal; both halves
    are checked here, against the real contract this repo owns.
    """
    run = await _run_chain(case)

    assert run.prepared.quarantine_reason is None, (
        f"[{case.chain_id}] wiring quarantined the handler before a single "
        f"message was dispatched: {run.prepared.quarantine_reason} "
        f"({run.prepared.quarantine_detail})"
    )

    assert not run.quarantine_messages, (
        f"[{case.chain_id}] {len(run.quarantine_messages)} message(s) landed in "
        f"{QUARANTINE_TOPIC}. This is the OMN-16767 failure signature: the chain "
        f"died silently into the quarantine sink while CI stayed green."
    )

    assert run.terminal_messages, (
        f"[{case.chain_id}] no terminal event reached {case.terminal_topic}. "
        f"The chain did not complete; a delegation on this chain would hang "
        f"until it timed out."
    )
