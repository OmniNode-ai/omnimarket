# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event Chain Gate — the projection WRITE seam (OMN-16831, OMN-16976).

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

What OMN-16976 added, and why the gate is parametrized.

The same refusal was caught again on 2026-08-29, on a different node and on the
onex-dev *cluster* runtime rather than the .201 compose lane: 32 occurrences in
60 minutes, twice per delegation, on
``HandlerProjectionDelegationInferenceResponse``. Its table is TENANT-classified
too, so it dies at the identical seam. It was invisible for the delegation
reasons above plus one more — ``GET /v1/tenants/me/delegations`` reads Postgres
directly rather than the projection, so a projection layer that has been 100%
dead for weeks produced no customer-visible symptom.

Delegation is 1 of **12** omnimarket contracts declaring ``schema: tenant``, so
this file is parametrized over cases: the seam is what is under gate, not the
node that happened to be diagnosed first.

RESOLVED (OMN-16997, 2026-08-30). The fix ``01261b796`` (#2976, 2026-08-28)
shipped in ``omnibase-infra`` **0.38.15**, and this repo now declares
``omnibase-infra>=0.38.16,<0.39.0``. Both seam rows PASS against the released
wheel, so the strict ``xfail`` tripwire that guarded this gate has been deleted
rather than left as a standing excuse — exactly the action its own reason string
instructed on the first XPASS. This is the automated notice OMN-16976 AC2 asked
for, and it has now fired and been acted on. The gate below is a plain
assertion again: if the tenant-authority seam ever refuses these writes a
second time, these rows go RED directly.

The second tripwire fired too (OMN-17549, 2026-09-02), and it is deleted here
for the same reason. ``_xfail_pending_tenant_registry_grant`` held the
``projection-delegation-write`` row at ``xfail(strict)`` while
``node_projection_delegation``'s OMN-16804 read of ``tenant_registry_mirror``
had no topology TABLE grant. On ``omnibase-infra`` 0.38.15 that row died at
wiring with::

    ValueError: Projection binding 'omninode_runtime_service' principal
    'omninode_runtime' lacks declared read privileges: SELECT on table
    omninode_internal.tenant_registry_mirror

0.38.16 ships the grant (``tenant_registry_mirror`` is in the
``omninode_runtime`` TABLE grant block of every topology instance), so all
three rows XPASS — which under ``strict=True`` is RED, which is exactly the
mechanical signal the marker's own reason string demanded. The marker is
deleted, not renewed.

What 0.38.16 also changes, and why this file gained a fourth gate row.

0.38.16 carries the OMN-17379 fail-closed projection guard. Before it, the
auto-wired projection dispatch callback caught every handler exception, logged
one ERROR line, best-effort-DLQ'd and **returned normally** — and a callback
that returns normally IS an ack, so the offset advanced past an event that
produced no row. That is how ``pr_merged_events`` acknowledged 230 merged PRs
into nothing while its consumer group reported ``Stable / TOTAL-LAG 0``. From
0.38.16 a *write-path* failure (dead connection, insufficient privilege,
missing relation, a wiring bug that denies the handler its adapter) raises
``ProjectionNotMaterializedError`` out of the subscriber callback so the offset
is withheld and the record is redelivered; a *content* failure (a
``ValidationError`` on a malformed payload) still DLQs and advances, because
redelivery can never repair the event.

Nothing in this file asserted that. Every row here is one-sided — it names a
refusal that must not happen — so the whole suite would have stayed green if
0.38.16 had gone back to silently acking a write that produced no row. That is
the same false-green shape the file was written against, one layer down, so
``test_a_write_that_produces_no_row_fails_closed`` asserts the guard directly:
the discard-port write fails, the callback RAISES, nothing lands on the DLQ,
and no terminal is emitted. It fails on 0.38.15, which is why the floor in
``pyproject.toml`` is ``>=0.38.16`` rather than ``>=0.38.15`` (OMN-17549).

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
from omnibase_infra.errors.error_projection import ProjectionNotMaterializedError
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


# The inference-response shape, reconstructed from a REAL event the onex-dev
# cluster runtime rejected on 2026-08-29 (OMN-16976). That refusal fired 32
# times in 60 minutes -- twice per delegation, a 100% failure rate -- and every
# one of them was quarantined on this contract's own DLQ topic:
#
#     Projection handler HandlerProjectionDelegationInferenceResponse routed
#     malformed/erroring event to DLQ
#     onex.dlq.omnimarket.projection-delegation-inference-response-malformed.v1:
#     ProjectionTenantContextError: [ONEX_CORE_081_OPERATION_FAILED]
#     Tenant projection has no cryptographically verified authority
#
# Every field here is one ModelInferenceResponseData declares. That model is
# `extra="forbid"`, so an invented key would be rejected as a validation error
# and this row would then be gating payload shape rather than the write seam.
#
# Why this row belongs in this file rather than a ticket comment: delegation is
# 1 of 12 omnimarket contracts declaring `schema: tenant`, and a gate that
# covers only the node that happened to be diagnosed first will not notice the
# other 11 regressing. This second row is the cheapest proof that the seam --
# not the node -- is what is under gate.
INFERENCE_RESPONSE_PROJECTION_CASE = ProjectionChainCase(
    chain_id="projection-delegation-inference-response-write",
    node_dir="node_projection_delegation_inference_response",
    entry_topic="onex.evt.omnibase-infra.inference-response.v1",
    dlq_topic=(
        "onex.dlq.omnimarket.projection-delegation-inference-response-malformed.v1"
    ),
    writer_handler_name="HandlerProjectionDelegationInferenceResponse",
    wire_payload={
        "correlation_id": "7a300828-4000-4000-8000-000000000002",
        "content": "ok",
        "model_used": "qwen3.8",
        "llm_call_id": "chatcmpl-omn16976",
        "latency_ms": 2300,
        "prompt_tokens": 53,
        "completion_tokens": 2,
        "total_tokens": 55,
        "tenant_id": "omninode",
    },
)


# Both rows drive the SAME seam. They are parametrized rather than duplicated so
# that a third tenant-classified projection is one tuple entry, not a new file.
PROJECTION_CHAIN_CASES = (
    DELEGATION_PROJECTION_CASE,
    INFERENCE_RESPONSE_PROJECTION_CASE,
)


@dataclass
class ProjectionChainRun:
    """What one projection chain execution produced, for assertion.

    Attributes:
        prepared: the real ``PreparedWiring`` for every handler the contract
            declares, in declaration order.
        handlers_entered: handler class names the real dispatch actually
            invoked, read out of the runtime's own log lines.
        dlq_failure_reasons: ``failure_reason`` of every envelope the dispatch
            routed to the contract's own DLQ topic.
        quarantine_messages: raw bytes of everything that reached the platform
            quarantine sink.
        terminal_messages: raw bytes published on the contract's terminal
            topic. OMN-13360 gates that terminal on ``rows_upserted >= 1``, so
            an empty tuple is the correct outcome for a failed write.
        not_materialized_failures: the ``ProjectionNotMaterializedError``
            messages that escaped the subscriber callback (OMN-17379). Read
            from the exception the in-memory bus records when a callback
            raises, so this is an observation of the real consume boundary
            rather than an injected spy. Non-empty means the offset was
            withheld; empty means the callback returned normally, which IS an
            ack.
    """

    prepared: tuple[PreparedWiring, ...]
    handlers_entered: tuple[str, ...]
    dlq_failure_reasons: tuple[str, ...]
    quarantine_messages: tuple[bytes, ...]
    terminal_messages: tuple[bytes, ...]
    not_materialized_failures: tuple[str, ...]


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
    # Only ``subscribe_topics`` is passed, which is what the subcontract is for
    # and what the kernel's own wiring uses it for. The publish side is already
    # carried by the DispatchResultApplier above, built from
    # ``contract.event_bus.publish_topics`` exactly as service_kernel builds it.
    #
    # OMN-16976: this is not cosmetic. Declaring the publish side here as well
    # would make the harness reject a contract the runtime accepts.
    # ``node_projection_delegation_inference_response`` publishes
    # ``onex.snapshot.projection.delegation.inference-response-text.v1``, which
    # ``ModelEventBusSubcontract`` refuses outright — its topic validator
    # demands exactly 5 segments (onex.kind.producer.event-name.version) and a
    # snapshot topic has 6. The kernel never routes that topic through this
    # model (it goes straight into DispatchResultApplier), so the contract is
    # live and working; a harness that validated it here would fail for a
    # reason production never encounters, and would have masked the tenant-seam
    # refusal this file exists to gate.
    await wiring.wire_subscriptions(
        ModelEventBusSubcontract(
            version=ModelSemVer(major=1, minor=0, patch=0),
            subscribe_topics=[case.entry_topic],
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

    # OMN-17379's whole mechanism is that the subscriber callback RAISES
    # instead of returning, because a callback that returns normally is an ack
    # and the offset advances past an event that produced no row. The in-memory
    # bus records a raising callback with ``logger.exception``, so the
    # exception object itself is on the log record — the raise is observed at
    # the real consume boundary, not inferred from a message string.
    not_materialized = tuple(
        str(record.exc_info[1])
        for record in caplog.records
        if record.exc_info is not None
        and isinstance(record.exc_info[1], ProjectionNotMaterializedError)
    )

    return ProjectionChainRun(
        prepared=tuple(prepared_wirings),
        handlers_entered=handlers_entered,
        dlq_failure_reasons=tuple(failure_reasons),
        quarantine_messages=tuple(quarantine_messages),
        terminal_messages=tuple(terminal_messages),
        not_materialized_failures=not_materialized,
    )


@pytest.mark.parametrize("case", PROJECTION_CHAIN_CASES, ids=lambda case: case.chain_id)
async def test_wiring_does_not_quarantine_the_projection_handlers(
    case: ProjectionChainCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No handler the contract declares may be quarantined before dispatch.

    A quarantined handler consumes offsets and drops every event, which is how
    OMN-16767 hid behind green CI. This is the cheapest half of the gate and
    it must never be allowed to regress.
    """
    run = await _run_projection_chain(case, monkeypatch, caplog)

    quarantined = [
        (w.handler_name, w.quarantine_reason, w.quarantine_detail)
        for w in run.prepared
        if w.quarantine_reason is not None
    ]
    assert not quarantined, (
        f"[{case.chain_id}] wiring quarantined "
        f"{len(quarantined)} handler(s) before a single message was "
        f"dispatched: {quarantined}"
    )
    assert not run.quarantine_messages, (
        f"[{case.chain_id}] "
        f"{len(run.quarantine_messages)} message(s) landed in "
        f"{QUARANTINE_TOPIC} — the chain died before reaching its handler"
    )


@pytest.mark.parametrize("case", PROJECTION_CHAIN_CASES, ids=lambda case: case.chain_id)
async def test_delegate_skill_terminal_reaches_the_projection_writer(
    case: ProjectionChainCase,
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
    run = await _run_projection_chain(case, monkeypatch, caplog)

    assert case.writer_handler_name in run.handlers_entered, (
        f"[{case.chain_id}] the real dispatch never entered "
        f"{case.writer_handler_name}. Handlers observed: "
        f"{run.handlers_entered or '(none)'}. A row is impossible if the "
        f"writing handler is never invoked — check subscription wiring, the "
        f"_event_type derived from the topic, and payload-type matching "
        f"before looking at the database."
    )


@pytest.mark.parametrize("case", PROJECTION_CHAIN_CASES, ids=lambda case: case.chain_id)
async def test_the_projection_write_is_not_refused_at_the_tenant_authority_seam(
    case: ProjectionChainCase,
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
    * OMN-15533 AC1/AC2 have no positive control to assert against;
    * for the inference-response row: `projection_delegation_inference_response_text`
      stays empty, the `inference-response-text.v1` snapshot the dashboard's
      DelegationModelOutputWidget subscribes to is never published, and the
      failure is masked from customers because `GET /v1/tenants/me/delegations`
      reads Postgres directly instead of the projection (OMN-16976).
    """
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


@pytest.mark.parametrize("case", PROJECTION_CHAIN_CASES, ids=lambda case: case.chain_id)
async def test_a_write_that_produces_no_row_fails_closed(
    case: ProjectionChainCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write-path failure must raise, not ack (OMN-17379).

    Every other row in this file is one-sided: it names a refusal that must not
    happen. None of them can tell a projection that wrote a row from one that
    consumed the event, wrote nothing, logged one ERROR line and returned — and
    a dispatch callback that returns normally IS an ack, so the offset advances
    and the fact is gone from the projection forever. That is not hypothetical:
    ``pr_merged_events`` acknowledged 230 merged PRs into nothing while its
    consumer group reported ``Stable / TOTAL-LAG 0 / CURRENT-OFFSET == LOG-END``
    (OMN-17379), and every external surface stayed green for 24 days.

    The DSNs point at the discard port, so the write here is *guaranteed* to
    fail. That is what makes this row a gate rather than a smoke test: the only
    question it asks is what the runtime does with a guaranteed write failure.

    Three things are asserted together, because any one of them alone can be
    satisfied by the silent-ack shape:

    1. ``ProjectionNotMaterializedError`` escaped the subscriber callback. This
       is the offset-withholding mechanism itself — ``EventBusKafka``
       classifies that type as offset-unsafe unconditionally and rewinds to the
       failed message's own offset, which is the only action that works under
       ``enable_auto_commit=True``.
    2. Nothing landed on the contract's DLQ topic. DLQ-and-advance is correct
       for a CONTENT failure (a malformed payload redelivery can never repair)
       and wrong for a WRITE-PATH failure (the event is valid and still owed a
       row). An ``OperationalError`` on connect is unambiguously the latter, so
       a DLQ record here would mean the pre-0.38.16 classification is back.
    3. No terminal was emitted. OMN-13360 gates
       ``projection-delegation-applied.v1`` on ``rows_upserted >= 1``; a
       terminal for a chain that wrote nothing would tell every downstream
       consumer the projection succeeded.

    This row is the reason ``pyproject.toml`` floors ``omnibase-infra`` at
    0.38.16 rather than 0.38.15: 0.38.15 has no guard to assert, so this test
    goes RED on it. A relock that dropped back to the permissive release could
    not pass this file, which is the point — the incompatibility is asserted
    instead of being carried as a silently-permissive lock (OMN-17549).
    """
    run = await _run_projection_chain(case, monkeypatch, caplog)

    assert case.writer_handler_name in run.handlers_entered, (
        f"[{case.chain_id}] the real dispatch never entered "
        f"{case.writer_handler_name}, so this row proves nothing about what the "
        f"runtime does with a failed write. Fix the wiring assertions above "
        f"before reading this one."
    )
    assert run.not_materialized_failures, (
        f"[{case.chain_id}] the write to the discard port failed and the "
        f"subscriber callback returned NORMALLY — that is an ack. The offset "
        f"advances past an event that produced no row, the projection silently "
        f"loses the fact, and the consumer group still reports lag 0. "
        f"omnibase-infra must raise ProjectionNotMaterializedError out of the "
        f"projection dispatch callback for a write-path failure (OMN-17379). "
        f"DLQ reasons observed: {run.dlq_failure_reasons or '(none)'}."
    )
    assert not run.dlq_failure_reasons, (
        f"[{case.chain_id}] a write-path failure was routed to "
        f"{case.dlq_topic} and the callback returned: "
        f"{run.dlq_failure_reasons}. DLQ-and-advance is correct only for a "
        f"CONTENT failure the event itself causes. A connection failure is the "
        f"RUNTIME's defect — the event is valid and still owed a row, so the "
        f"offset must be withheld and the record redelivered (OMN-17379)."
    )
    assert not run.terminal_messages, (
        f"[{case.chain_id}] {len(run.terminal_messages)} terminal event(s) "
        f"were published for a chain that wrote zero rows. OMN-13360 gates the "
        f"terminal on rows_upserted >= 1; emitting one here tells every "
        f"downstream consumer a projection succeeded when it materialized "
        f"nothing."
    )
