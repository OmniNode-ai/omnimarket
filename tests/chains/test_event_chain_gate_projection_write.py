# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event Chain Gate — the projection WRITE seam (OMN-16831).

Why this suite exists, separately from ``test_event_chain_gate.py``.

That file gates chains whose terminal is an EVENT: a message goes in, a typed
model comes out on a declared topic, and the whole thing is provable with no
database. This file gates the other half — chains whose terminal is a durable
ROW. ``node_projection_delegation`` is one: its contract's own ``golden_path``
says "projection handler upserts row into delegation_events table", and its
terminal event ``onex.evt.omnimarket.projection-delegation-applied.v1`` is
gated by the runtime on ``rows_upserted >= 1`` (OMN-13360). No row, no terminal.

The outage this gate is written from.

``omnidash_analytics.delegation_events`` held **zero** rows on the .201 dev lane
while delegations completed successfully end to end — two of them on
2026-08-28 with real inference and real token counts. The node was subscribed,
the topic addressing was right, the payload validated, and
``HandlerProjectionDelegation.handle()` was entered. It then died on its first
touch of the database:

    ProjectionTenantContextError: [ONEX_CORE_081_OPERATION_FAILED]
    Tenant projection has no cryptographically verified authority

``delegation_events`` is classified TENANT domain by this repo's contract
(``db_io.db_tables[].schema: tenant``, OMN-15423). Every TENANT-domain read and
write in the runtime goes through ``TenantProjectionTableOperation``, which
demands a ``VerifiedProjectionTenantAuthority``. That capability has a sealed
constructor and is mintable only by verifying a signed ``ModelMessageEnvelope``
— and ``bind_projection_tenant_authority`` has zero non-test call sites in the
shipped ``omnibase_infra`` package. This repo already asserts that absence on
purpose, in ``tests/unit/projection/test_house_tenant_default_ratchet.py``.

So the write path is not slow, not flaky and not misconfigured. It is
structurally unreachable, and it fails the way the OMN-16767 outage failed: the
dispatch reports ``SUCCESS``, the caller keeps its 202, one ERROR line is
logged, the event goes to a DLQ topic nobody watches, and CI stays green in
every repo.

Why no existing test caught it.

Every projection test in this repo injects its own database adapter. An
injected adapter has no domain classification, no topology binding and no
tenant enforcement, so it cannot reproduce a refusal that only the REAL
``ProjectionDatabaseOperations`` performs. This suite builds that real adapter
from the real contract and the real topology, which is the only way the seam
gets exercised at all.

What is real here.

Everything except the database server. The real ``contract.yaml`` on disk, the
real ``discover_contracts_from_paths``, the real ``_prepare_handler_wiring``
arm selection for BOTH handlers the contract declares, the real
``MessageDispatchEngine``, the real ``EventBusInmemory``, the real
``ProjectionDatabaseOperations`` with its real domain-classified table
operations, and the real handler classes resolved from the module paths the
contract names. Nothing is mocked, patched or stubbed.

The DSNs point at the discard port. That is deliberate and it is not a
weakening: the refusal under gate happens *before* a connection is opened, so
a reachable server would change nothing about what this proves — it would only
add a Postgres dependency to a merge-gating job, and an integration-marked
test that skips on the merge gate is exactly the silent-skip shape this repo
already refuses (``Integration Silent-Skip Guard``, OMN-14172).
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
    _resolve_projection_database_target,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    EventBusSubcontractWiring,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.service_dispatch_result_applier import DispatchResultApplier
from omnibase_infra.topology import load_topology_profile
from omnibase_spi.protocols.runtime import ProtocolDispatchEngine

# Same placement rationale as tests/chains/test_event_chain_gate.py: in this
# repo `integration` means a REAL Kafka bus, and this gate is broker-free. It
# needs no service of any kind, so it is a plain unit test.
pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# The platform quarantine sink — where a chain that dies before reaching its
# handler ends up.
QUARANTINE_TOPIC = "onex.dlq.omnibase-infra.quarantine.v1"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"

# Syntactically valid, deliberately unreachable: the discard port. See the
# module docstring — the refusal under gate precedes any connect(), so this
# buys hermeticity without weakening what the gate proves.
_UNREACHABLE_DSN = "postgresql://gate:gate@127.0.0.1:9/gate"

# The substring the runtime's own refusal carries. Matched on the DLQ
# envelope's ``failure_reason``, which is where the projection dispatch
# callback records why it dropped the event.
_TENANT_AUTHORITY_REFUSAL = "no cryptographically verified authority"


@dataclass(frozen=True)
class ProjectionChainCase:
    """One projection chain driven end to end through the real dispatch seam.

    Attributes:
        chain_id: pytest id; also the consumer-group node name, so a failure
            names the chain.
        node_dir: directory under ``src/omnimarket/nodes`` whose REAL
            ``contract.yaml`` is discovered and wired.
        entry_topic: the topic the raw wire bytes are published to. It must be
            one the contract actually subscribes to — asserted, not assumed.
        dlq_topic: the contract's own ``event_bus.dlq_topics[0]``, where the
            projection dispatch callback routes an erroring event.
        writer_handler_name: the class the dispatch must actually reach for a
            row to be possible. Named so the non-vacuity assertion cannot pass
            on a chain that never entered a writer at all.
        wire_payload: the envelope's ``payload`` as JSON-safe primitives,
            exactly as it arrives off the wire.
    """

    chain_id: str
    node_dir: str
    entry_topic: str
    dlq_topic: str
    writer_handler_name: str
    wire_payload: dict[str, object]

    @property
    def contract_path(self) -> Path:
        return _SRC_ROOT / self.node_dir / "contract.yaml"


# The delegate-skill terminal shape, copied from a REAL terminal observed on
# the .201 dev lane on 2026-08-28 (correlation 78cbeb63-…, qwen3.8, 53/2
# tokens). Using a real terminal matters: `task_type` is distinct from
# `model_name` and the token counts are non-zero, so a row produced from it is
# a positive control for OMN-15533 AC1/AC2 rather than a shape-only fixture.
DELEGATION_PROJECTION_CASE = ProjectionChainCase(
    chain_id="projection-delegation-write",
    node_dir="node_projection_delegation",
    entry_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
    dlq_topic="onex.dlq.omnimarket.projection-delegation-malformed.v1",
    writer_handler_name="HandlerProjectionDelegation",
    wire_payload={
        "correlation_id": "7a300828-4000-4000-8000-000000000001",
        "session_id": "7a300828-4000-4000-8000-000000000001",
        "status": "completed",
        "task_type": "code_generation",
        "model_name": "qwen3.8",
        "provider": "http://127.0.0.1:9/v1/chat/completions",
        "response_text": "ok",
        "metrics": {"input_tokens": 53, "output_tokens": 2, "total_tokens": 55},
        "duration_ms": 2300,
        "emitted_at": "2026-08-28T00:00:00+00:00",
    },
)


@dataclass
class ProjectionChainRun:
    """What one projection chain execution produced, for assertion."""

    prepared: tuple[PreparedWiring, ...]
    handlers_entered: tuple[str, ...]
    dlq_failure_reasons: tuple[str, ...]
    quarantine_messages: tuple[bytes, ...]
    terminal_messages: tuple[bytes, ...]


def _bind_unreachable_dsns(
    case: ProjectionChainCase,
    contract: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point every DSN the contract's topology bindings require at the discard port.

    The env var NAMES are read out of the resolved topology rather than
    hardcoded, so a topology rename cannot silently turn this gate into a
    ValueError about missing bindings and thereby stop testing the seam.
    """
    db_io = getattr(contract, "db_io", None)
    assert db_io is not None, (
        f"[{case.chain_id}] the contract declares no db_io — this gate is for "
        f"projection chains whose terminal is a durable row"
    )
    target = _resolve_projection_database_target(
        db_io.db_tables, load_topology_profile("local")
    )
    for binding in target.bindings:
        monkeypatch.setenv(binding.dsn_env, _UNREACHABLE_DSN)


async def _run_projection_chain(
    case: ProjectionChainCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> ProjectionChainRun:
    """Drive one projection chain end to end from the REAL contract on disk.

    Both handlers the contract declares are prepared and registered, in
    declaration order, exactly as the kernel's own contract-wiring loop does.
    Registering only the first would test a wiring this runtime never builds.
    """
    assert case.contract_path.exists(), (
        f"[{case.chain_id}] contract not found at {case.contract_path} — the "
        f"node was renamed or removed and this gate row is stale"
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
    assert case.entry_topic in contract.event_bus.subscribe_topics, (
        f"[{case.chain_id}] {case.entry_topic} is not in the contract's "
        f"subscribe_topics — this gate row would prove nothing about the "
        f"chain the runtime actually wires"
    )

    _bind_unreachable_dsns(case, contract, monkeypatch)

    bus = EventBusInmemory(environment="chain-gate", group="chain-gate")
    await bus.start()

    dlq_messages: list[bytes] = []
    quarantine_messages: list[bytes] = []
    terminal_messages: list[bytes] = []

    async def _collect_dlq(message: object) -> None:
        dlq_messages.append(cast("bytes", getattr(message, "value", b"")))

    async def _collect_quarantine(message: object) -> None:
        quarantine_messages.append(cast("bytes", getattr(message, "value", b"")))

    async def _collect_terminal(message: object) -> None:
        terminal_messages.append(cast("bytes", getattr(message, "value", b"")))

    terminal_topic = contract.event_bus.publish_topics[0]
    await bus.subscribe(
        case.dlq_topic,
        on_message=_collect_dlq,
        group_id=f"chain-gate-dlq-{case.chain_id}",
    )
    await bus.subscribe(
        QUARANTINE_TOPIC,
        on_message=_collect_quarantine,
        group_id=f"chain-gate-quarantine-{case.chain_id}",
    )
    await bus.subscribe(
        terminal_topic,
        on_message=_collect_terminal,
        group_id=f"chain-gate-terminal-{case.chain_id}",
    )

    engine = MessageDispatchEngine()

    # ---- THE SEAM UNDER GATE: real arm selection from the real contract ----
    prepared_wirings: list[PreparedWiring] = []
    for entry in contract.handler_routing.handlers:
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
            # refused outright without one.
            topology=load_topology_profile("local"),
        )
        prepared_wirings.append(prepared)
        engine.register_dispatcher(
            dispatcher_id=prepared.dispatcher_id,
            dispatcher=prepared.dispatcher,
            category=prepared.category,
            message_types=prepared.message_types,
        )
        engine.register_route(
            ModelDispatchRoute(
                route_id=f"{case.chain_id}-{entry.operation}",
                topic_pattern=case.entry_topic,
                message_category=prepared.category,
                # NOTE: the field is ``handler_id``, not ``dispatcher_id``.
                handler_id=prepared.dispatcher_id,
            )
        )
    engine.freeze()

    applier = DispatchResultApplier(
        event_bus=cast("ProtocolEventBusLike", bus),
        output_topic=terminal_topic,
        output_topic_map={},
        allowed_output_topics=(terminal_topic,),
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
            publish_topics=[terminal_topic],
        ),
        case.chain_id,
    )

    # ---- The wire message, exactly as Kafka delivers it: raw JSON bytes ----
    with caplog.at_level("DEBUG", logger="omnibase_infra.runtime.auto_wiring"):
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

    # The runtime names the handler it invoked in every projection-dispatch log
    # line it emits (completed, wrote-zero-rows, and both error branches). That
    # is a real observation of the real dispatch, not an injected spy.
    handlers_entered = tuple(
        sorted(
            {
                name
                for name in {w.handler_name for w in prepared_wirings}
                for record in caplog.records
                if f"handler={name}" in record.getMessage()
            }
        )
    )

    failure_reasons: list[str] = []
    for raw in dlq_messages:
        try:
            failure_reasons.append(str(json.loads(raw).get("failure_reason", "")))
        except (ValueError, AttributeError):
            failure_reasons.append(raw.decode("utf-8", errors="replace"))

    return ProjectionChainRun(
        prepared=tuple(prepared_wirings),
        handlers_entered=handlers_entered,
        dlq_failure_reasons=tuple(failure_reasons),
        quarantine_messages=tuple(quarantine_messages),
        terminal_messages=tuple(terminal_messages),
    )


async def test_wiring_does_not_quarantine_the_projection_handlers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No handler the contract declares may be quarantined before dispatch.

    A quarantined handler consumes offsets and drops every event, which is how
    OMN-16767 hid behind green CI. This is the cheapest half of the gate and
    it must never be allowed to regress.
    """
    run = await _run_projection_chain(DELEGATION_PROJECTION_CASE, monkeypatch, caplog)

    quarantined = [
        (w.handler_name, w.quarantine_reason, w.quarantine_detail)
        for w in run.prepared
        if w.quarantine_reason is not None
    ]
    assert not quarantined, (
        f"[{DELEGATION_PROJECTION_CASE.chain_id}] wiring quarantined "
        f"{len(quarantined)} handler(s) before a single message was "
        f"dispatched: {quarantined}"
    )
    assert not run.quarantine_messages, (
        f"[{DELEGATION_PROJECTION_CASE.chain_id}] "
        f"{len(run.quarantine_messages)} message(s) landed in "
        f"{QUARANTINE_TOPIC} — the chain died before reaching its handler"
    )


async def test_delegate_skill_terminal_reaches_the_projection_writer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The writing handler must actually be entered by the real dispatch.

    This is the non-vacuity control for the seam assertion below: a gate that
    only says "no tenant-authority refusal happened" would pass trivially on a
    chain that never reached a writer at all. It also pins the three hops that
    DO work today — subscription, topic addressing, payload matching — so a
    future regression in any of them is attributed correctly instead of being
    re-diagnosed as the write-seam defect.
    """
    case = DELEGATION_PROJECTION_CASE
    run = await _run_projection_chain(case, monkeypatch, caplog)

    assert case.writer_handler_name in run.handlers_entered, (
        f"[{case.chain_id}] the real dispatch never entered "
        f"{case.writer_handler_name}. Handlers observed: "
        f"{run.handlers_entered or '(none)'}. A row is impossible if the "
        f"writing handler is never invoked — check subscription wiring, the "
        f"_event_type derived from the topic, and payload-type matching "
        f"before looking at the database."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OMN-16831: the delegation projection write is refused at the "
        "tenant-authority seam. `delegation_events` is classified TENANT "
        "domain by this repo's contract, so every read and write goes through "
        "TenantProjectionTableOperation, which requires a "
        "VerifiedProjectionTenantAuthority — a sealed capability that "
        "`bind_projection_tenant_authority` never binds anywhere in shipped "
        "omnibase_infra (asserted on purpose by "
        "tests/unit/projection/test_house_tenant_default_ratchet.py). STRICT: "
        "this turns RED (XPASS) the moment that is reconciled, which is the "
        "signal to delete this marker."
    ),
)
async def test_the_projection_write_is_not_refused_at_the_tenant_authority_seam(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delegation terminal must reach the database, not die before it.

    The assertion is deliberately one-sided — it names the refusal, not the
    success. A row cannot be asserted here without a Postgres server, and an
    integration-marked test that skips on the merge gate would not be a gate
    at all. What CAN be proven with no server is that the event is not refused
    *before* a connection is ever opened, which is exactly the defect: the
    chain never gets as far as the database to fail there.

    Consequences of this being RED, so nobody has to re-derive them:

    * `omnidash_analytics.delegation_events` stays at zero rows on every lane;
    * the `projection-delegation-applied.v1` terminal never fires, because
      OMN-13360 gates it on `rows_upserted >= 1`;
    * the dashboard's delegation surfaces and the `event_sessions` branch of
      `projection_delegation_savings` have no input;
    * OMN-15533 AC1/AC2 have no positive control to assert against.
    """
    case = DELEGATION_PROJECTION_CASE
    run = await _run_projection_chain(case, monkeypatch, caplog)

    refusals = [
        reason
        for reason in run.dlq_failure_reasons
        if _TENANT_AUTHORITY_REFUSAL in reason
    ]
    assert not refusals, (
        f"[{case.chain_id}] the projection write was refused before reaching "
        f"the database: {refusals}. The event was routed to {case.dlq_topic} "
        f"while the dispatch reported SUCCESS, so the caller keeps its 202 "
        f"and no row is written. See OMN-16831."
    )
