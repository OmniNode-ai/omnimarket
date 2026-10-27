# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16874 / OMN-16875 — the writer's dispatch contract with the runtime.

``ConsumerFlowProjectionWriter`` is dispatched **in-process** by the runtime's
auto-wiring, once per consumed message, through a synchronous ``handle()``.
Three obligations follow from that:

1. **Every loop-bound resource is scoped to one loop.** An asyncpg pool (or an
   ``AIOKafkaProducer``) is bound to the loop that created it. When
   ``handle()`` called ``asyncio.run``, a pool cached across calls belonged to
   a loop that no longer existed. Live on the lab dev lane on 2026-08-28:
   34 ``RuntimeError: Event loop is closed``, ``consumer_flow_windows`` at 0
   rows, DLQ at 36 and climbing. Two consecutive messages are driven here
   because one cannot expose this.

1a. **That loop, and the pool on it, outlive the message (OMN-17427, C28).**
   The fix for (1) opened a new loop, pool and producer for every heartbeat;
   on 2026-10-22 the writer projected about 6 heartbeats a minute against about
   6 arriving and sat 5.5 hours behind. The writer now keeps one dispatch loop
   for its life, so N messages open one pool.

2. **The return value is the applied event's payload.** ``handler_wiring``
   publishes the handler's result onto
   ``onex.evt.omnimarket.projection-consumer-flow-applied.v1`` (OMN-16875), so a
   bare ``{"projected": True}`` ack discarded every window fact at the producer
   and left the downstream stall alert with nothing to evaluate.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    ConsumerFlowProjectionWriter,
)

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_STALLED_GROUP = "local.omnimarket.node_registration_orchestrator.consume"


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for ``AsyncpgAdapter`` and enforces its loop affinity."""

    def __init__(self) -> None:
        self._pool: _LoopBoundPool | None = None
        self.connects = 0
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()
        self.connects += 1

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.rows.append((query, params))
        if "SELECT COUNT(*)" in query:
            # One answer per probed (topic, window) the statement was bound with.
            return [
                {"ordinal": ordinal, "window_count": 0, "produced": 0}
                for ordinal in range(1, len(params[0]) + 1)
            ]
        if "MAX(ingest_sequence)" in query:
            return [{"last_sequence": None}]
        if "RETURNING" in query:
            return [_written_row()]
        return []


def _written_row() -> dict[str, Any]:
    return {
        "consumer_group": _STALLED_GROUP,
        "topic": _IN_TOPIC,
        "window_start": _T0,
        "window_end": _T0 + timedelta(seconds=60),
        "node_id": str(_NODE_ID),
        "ingest_sequence": 1,
        "projection_cursor": 1,
        "messages_in": 229150,
        "messages_out": 0,
        "messages_dlq": 0,
        "handler_errors": 0,
        "upstream_produced": None,
        "upstream_evidence": "UNOBSERVED",
        "flow_state": "STALLED",
        "evaluated_at": _T0 + timedelta(seconds=60),
    }


_NODE_ID = uuid4()


def _heartbeat(sequence: int) -> dict[str, Any]:
    start = _T0 + timedelta(seconds=60 * sequence)
    return {
        "flow_window": {
            "node_id": str(_NODE_ID),
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(seconds=60)).isoformat(),
            "window_sequence": sequence,
            "consumer_deltas": [
                {
                    "consumer_group": _STALLED_GROUP,
                    "topic": _IN_TOPIC,
                    "node_id": str(_NODE_ID),
                    "window_start": start.isoformat(),
                    "window_end": (start + timedelta(seconds=60)).isoformat(),
                    "window_sequence": sequence,
                    "messages_in": 229150,
                    "messages_out": 0,
                    "messages_dlq": 0,
                    "handler_errors": 0,
                }
            ],
            "produce_deltas": [],
        },
        "_topic": _IN_TOPIC,
    }


@pytest.fixture
def writer(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ConsumerFlowProjectionWriter]:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = ConsumerFlowProjectionWriter()
    instance._db = _RecordingAdapter()  # type: ignore[assignment]
    # The snapshot publish is a separate seam with its own transport; this test
    # is about the DB write path and the returned facts.
    instance._snapshot_exposure = None
    yield instance
    instance.close_dispatch()


@pytest.mark.unit
def test_writer_declares_the_in_process_dispatch_capability() -> None:
    """The runtime routes on a declared capability, never on the class name.

    OMN-16874: ``handler_wiring._is_standalone_projection_runner`` used to test
    ``type(h).__name__.endswith("ProjectionRunner")``. This class is named
    ``...Writer`` because the OMN-14350 type-word ratchet hard-fails ``Runner``,
    so it silently fell out of that gate onto the other dispatch branch. The
    intent is now stated rather than inferred from a spelling.
    """
    assert ConsumerFlowProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.unit
def test_no_sibling_projection_runner_claims_in_process_dispatch() -> None:
    """Exactly one runner-shaped class opts in, and every sibling stays out.

    The capability is what moves a class between the runtime's two projection
    dispatch branches, so a stray declaration on a ``*ProjectionRunner`` would
    double-dispatch a projection that already runs in-process through its own
    pure handler — writing every row twice. This walks the real contracts rather
    than a hand-kept list, so a new projection node is covered the day it lands.
    """
    import importlib
    import pathlib

    import yaml

    attr = "onex_runtime_inprocess_dispatch"
    nodes = pathlib.Path(__file__).parent.parent / "src/omnimarket/nodes"
    declared: set[str] = set()
    checked = 0

    for node_dir in sorted(nodes.glob("node_projection_*")):
        contract = node_dir / "contract.yaml"
        if not contract.exists():
            continue
        routing = yaml.safe_load(contract.read_text()).get("handler_routing") or {}
        declared_here: set[str] = set()
        for entry in routing.get("handlers", []):
            ref = entry.get("handler") or {}
            name, module = ref.get("name"), ref.get("module")
            if not name or not module:
                continue
            klass = getattr(importlib.import_module(module), name, None)
            if klass is None:
                continue
            checked += 1
            if not getattr(klass, attr, False):
                continue
            declared.add(name)
            declared_here.add(name)
            # The hazard is a declaration on the operation that ALREADY runs
            # in-process through the node's own pure handler: that is the
            # double-dispatch this capability exists to prevent. Only the
            # node's standalone runner operation may carry it.
            # Both suffixes name a node's STANDALONE writer operation, and the
            # catalog uses both: node_projection_consumer_flow and
            # node_projection_runtime_error_fingerprints end theirs
            # `_projection_runner`, node_projection_runner_fleet (OMN-18768)
            # ends its `_projection_writer`. An earlier revision of this branch
            # accepted only the first and so refused a writer that is correct,
            # which the OMN-18768 merge caught. What is actually being refused
            # is the PURE HANDLER's operation, spelled `projection_<name>`:
            # declaring the capability there is the double dispatch that writes
            # every row twice.
            operation = str(entry.get("operation", ""))
            assert operation.endswith(("_projection_runner", "_projection_writer")), (
                f"{name} declares {attr} on operation {operation!r}, which is "
                "not the node's standalone writer operation"
            )

        assert len(declared_here) <= 1, (
            f"{node_dir.name} has {len(declared_here)} handlers declaring "
            f"{attr}: {sorted(declared_here)} — a projection dispatched twice "
            "writes every row twice"
        )

    assert checked > 0, "the contract walk found no handlers — the test is inert"
    # The set is pinned, not open: the hazard this guards is a declaration on a
    # class that ALREADY runs in-process through its own pure handler, which
    # would write every row twice. It is not "only one class may ever opt in",
    # so the set grows by one reviewed line per genuine projection WRITER.
    #
    # FleetLivenessProjectionWriter (OMN-18768) is the second. It is the same
    # shape as the first and for the same reason: it is the node's DB writer,
    # dispatched once per consumed message by the runtime auto-wiring rather
    # than driven by its own consume loop, and it opens its asyncpg pool and
    # its snapshot producer inside the per-message loop precisely because of
    # that. Its node's pure reducer, HandlerProjectionRunnerFleet, does NOT
    # declare the capability and must not -- that is the double-dispatch this
    # test exists to refuse.
    #
    # RuntimeErrorFingerprintProjectionWriter (OMN-18770) is the third, on
    # the same reviewed terms. Its node's pure reducer,
    # HandlerProjectionRuntimeErrorFingerprints, does NOT declare the
    # capability. An earlier revision of this branch replaced the pinned set
    # with a membership check to avoid editing a roster per node; that
    # weakened the gate from "exactly these writers" to "at least this one",
    # so the pinned form is kept and this line is the edit it asks for.
    #
    # LabLaneHealthProjectionWriter (OMN-18769) is the fourth, on the same
    # reviewed terms and for the reason the roster exists: without the
    # declaration the runtime routed this node down the OTHER branch, which
    # validated the event and returned without writing, so the lane consumed
    # and committed offsets while the table stayed empty. Its node's pure
    # reducer, HandlerProjectionLabLaneHealth, does NOT declare the capability.
    #
    # DodVerdictProjectionWriter (OMN-18900) is the fifth, on the same
    # reviewed terms. It is the node's DB writer, dispatched once per consumed
    # verdict by the runtime auto-wiring, and it has no dedicated writer
    # Deployment anywhere -- so undeclared it would take the STANDALONE branch,
    # be dispatched by nobody, and leave the definition-of-done verdict exactly
    # as undurable as it was before the node existed. Its node's pure fold,
    # HandlerProjectionDodVerdict, does NOT declare the capability.
    #
    # ProdPromotionGateProjectionWriter (OMN-18999) is the sixth, on the same
    # reviewed terms. It is the node's DB writer, dispatched once per consumed
    # gate decision by the runtime auto-wiring, and it has no dedicated writer
    # Deployment anywhere -- so undeclared it would take the STANDALONE branch,
    # be dispatched by nobody, and leave a blocked prod promotion exactly as
    # unqueryable as it was before the node existed. Its node's pure fold,
    # HandlerProjectionProdPromotionGate, does NOT declare the capability.
    #
    # CiAttemptOutcomeProjectionWriter (OMN-18903) is the seventh, on the same
    # reviewed terms. It is its node's DB writer, dispatched once per consumed
    # message by the runtime auto-wiring, and it opens its asyncpg pool inside
    # the per-message loop for that reason. Its node's pure fold,
    # HandlerProjectionCiAttemptOutcome, does NOT declare the capability and
    # must not. This node has no dedicated writer deployment either, so without
    # the declaration it would be dispatched by nobody at all -- the same
    # failure the OMN-18900 entry directly above records, reached by the same
    # route, which is why both entries arrive in the same merge.
    #
    # ClaudeHookEventsProjectionWriter (OMN-19513) is the eighth, on the same
    # reviewed terms. It is the node's DB writer, dispatched once per consumed
    # hook event by the runtime auto-wiring, and it opens its asyncpg pool
    # inside the per-message loop for that reason. This node has no dedicated
    # writer deployment, so undeclared it would be dispatched by nobody and
    # every captured Claude Code hook event would be consumed with zero rows.
    # Its node's pure fold, HandlerProjectionClaudeHookEvents, does NOT
    # declare the capability and must not.
    #
    # PrLandingProjectionWriter (OMN-19833) is the next, on the same reviewed
    # terms. It is the only writer of pr_landing_state and
    # pr_landing_transitions, dispatched once per consumed landing event by the
    # runtime auto-wiring, with no dedicated writer deployment -- so undeclared
    # it would be dispatched by nobody. Its node's pure fold,
    # HandlerProjectionPrLanding, does NOT declare the capability.
    #
    # LabJobProjectionWriter (OMN-20604 M2) is the only writer of
    # lab_job_state and lab_job_transitions, dispatched once per transitioned
    # event by runtime auto-wiring. Its pure fold, HandlerProjectionLabJob,
    # does NOT declare the capability; there is no dedicated deployment.
    #
    # SessionContentProjectionWriter (OMN-19550) follows, on the same reviewed
    # terms. It is the node's DB writer, dispatched once per consumed
    # content record by the runtime auto-wiring, and it has no dedicated writer
    # deployment, so undeclared it would take the standalone branch and be
    # dispatched by nobody. Its node's pure fold,
    # HandlerProjectionSessionContent, does NOT declare the capability and must
    # not.
    #
    # WorkLedgerProjectionWriter (OMN-19513) is the work-ledger projection's
    # rule 7a effect-class DB writer. It is dispatched once per consumed row by
    # runtime auto-wiring; its pure fold, HandlerProjectionWorkLedger, does not
    # declare the capability.
    # WorktreeReconcileProjectionWriter (OMN-19399) follows, on the same
    # reviewed terms. It is the only writer of worktree_reconcile_hosts,
    # dispatched once per consumed run-completed event by runtime auto-wiring,
    # with one upsert keyed on host and ordered by finished_at. It has no
    # dedicated writer deployment, so undeclared it would be dispatched by
    # nobody. Its node's pure fold, HandlerProjectionWorktreeReconcile, does
    # NOT declare the capability.
    #
    # LabContainerMemoryProjectionWriter (OMN-19961) follows, on the same
    # reviewed terms. It is the only writer of lab_container_memory_window,
    # dispatched once per consumed memory event by the runtime auto-wiring,
    # with no dedicated writer deployment -- so undeclared it would be
    # dispatched by nobody. Its node's pure fold, HandlerContainerMemoryFold,
    # does NOT declare the capability.
    #
    # BoardProbeResultsProjectionWriter (OMN-19937) is the node's DB writer,
    # dispatched once per consumed probe result by the runtime auto-wiring,
    # with no dedicated writer deployment. Its pure fold,
    # board_probe_results_fold, does NOT declare the capability.
    #
    # LabProofReceiptsProjectionWriter (omnimarket#3043 / OMN-19566) follows
    # under rule 7a / OMN-18769: the runtime dispatches the DB writer once per
    # consumed lab-proof receipt, and the writer calls its pure fold in process.
    # HandlerProjectionLabProofReceipts does NOT declare the capability, so the
    # receipt is not dispatched to both the writer and the fold.
    # PrStateProjectionWriter (OMN-19999) follows the same pattern. It is the
    # node's DB writer, dispatched once per consumed PR-state event by runtime
    # auto-wiring; its pure fold, HandlerProjectionPrState, does not declare
    # this capability and must not.
    # DelegationEvalProjectionWriter (OMN-19790) is the label-store writer,
    # dispatched once per consumed labelled-item event by runtime auto-wiring.
    # ProviderQuotaProjectionWriter (OMN-20154) is the quota-state writer,
    # dispatched once per consumed provider-quota observation.
    #
    # RunTraceProjectionWriter (OMN-19987) follows, on the same reviewed terms.
    # It is the only writer of run_trace, dispatched once per consumed run-step
    # event by the runtime auto-wiring, with no dedicated writer deployment, so
    # undeclared it would be dispatched by nobody. Its node's pure fold,
    # HandlerProjectionRunTrace, does NOT declare the capability.
    #
    # UsageByModelDayProjectionWriter (OMN-19978) follows, on the same reviewed
    # terms. It is the node's DB writer, dispatched once per consumed usage
    # record by the runtime auto-wiring, and it has no dedicated writer
    # deployment, so undeclared it would take the standalone branch and be
    # dispatched by nobody. Its node's pure fold,
    # HandlerProjectionUsageByModelDay, does NOT declare the capability.
    #
    # AlertChannelLivenessProjectionWriter follows, on the same reviewed terms.
    # It is the only writer of alert_channel_liveness_verdicts, dispatched once
    # per consumed liveness result by runtime auto-wiring, with no dedicated
    # writer deployment. Its pure fold, HandlerProjectionAlertChannelLiveness,
    # does NOT declare the capability.
    # HandlerRoutingFeedbackWriter (OMN-20575) is the routing-feedback DB
    # writer, dispatched once per consumed reducer output by runtime
    # auto-wiring; the reducer's pure fold is a separate node and does NOT
    # declare the capability.
    # OMN-20802: the runtime dispatches this writer once; its definition-B
    # fold, HandlerProjectionAutomationLiveness, has no dispatch declaration.
    # LocalEnvironmentProjectionWriter (OMN-19988) is the only writer of
    # local_environment, dispatched once per consumed environment report by
    # runtime auto-wiring; its pure plan, HandlerProjectionLocalEnvironment,
    # does NOT declare the capability.
    # CompactionReceiptsProjectionWriter (OMN-21336) is the only writer of
    # compaction_receipts, dispatched once per consumed install-confirm-completed event by
    # runtime auto-wiring; its pure fold, HandlerProjectionCompactionReceipts,
    # does NOT declare the capability.
    # HandlerDelegationCapabilityMapWriter (OMN-20169) is the only writer of
    # delegation_capability_cells, dispatched once per consumed map-run event
    # by runtime auto-wiring; its pure fold, HandlerDelegationCapabilityMap,
    # and the offline HandlerCapabilityGridRun do NOT declare the capability.
    # OMN-21288 folds five more writers into the runtime on the same reviewed
    # terms: LiveEventsProjectionWriter, RegistrationProjectionWriter,
    # SavingsProjectionWriter, TenantCredentialsProjectionWriter and
    # TenantMirrorProjectionWriter. Each is its node's only DB writer,
    # dispatched once per consumed event by runtime auto-wiring on its
    # standalone writer operation; the per-node walk above proves no pure
    # handler in those nodes also declares the capability.
    assert declared == {
        "AlertChannelLivenessProjectionWriter",
        "AutomationLivenessProjectionWriter",
        "BoardProbeResultsProjectionWriter",
        "ClaudeHookEventsProjectionWriter",
        "CompactionReceiptsProjectionWriter",
        "MeteringSummaryProjectionWriter",
        "CiAttemptOutcomeProjectionWriter",
        "ConsumerFlowProjectionWriter",
        "LocalEnvironmentProjectionWriter",
        "DelegationEvalProjectionWriter",
        "HandlerDelegationDispositionWriter",
        "HandlerDelegationCapabilityMapWriter",
        "ProviderQuotaProjectionWriter",
        "DodVerdictProjectionWriter",
        "FleetLivenessProjectionWriter",
        "LabContainerMemoryProjectionWriter",
        "LabLaneHealthProjectionWriter",
        "LabProofReceiptsProjectionWriter",
        "PrLandingProjectionWriter",
        "LabJobProjectionWriter",
        "PrStateProjectionWriter",
        "ProdPromotionGateProjectionWriter",
        "RuntimeErrorFingerprintProjectionWriter",
        "SessionContentProjectionWriter",
        "TopicActivityProjectionWriter",
        "WorkLedgerProjectionWriter",
        "WorktreeReconcileProjectionWriter",
        "UsageByModelDayProjectionWriter",
        "HandlerRoutingFeedbackWriter",
        "RunTraceProjectionWriter",
        "LiveEventsProjectionWriter",
        "RegistrationProjectionWriter",
        "SavingsProjectionWriter",
        "TenantCredentialsProjectionWriter",
        "TenantMirrorProjectionWriter",
    }, f"unexpected {attr} declarations: {sorted(declared)}"


@pytest.mark.unit
def test_two_consecutive_messages_both_write(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """A second real write must not die on a loop the first one closed."""
    first = writer.handle(_heartbeat(1))
    second = writer.handle(_heartbeat(2))

    adapter = writer.db
    assert adapter.connects == 1, (  # type: ignore[attr-defined]
        "the second message must reuse the pool the first one opened on the "
        "writer's dispatch loop"
    )
    assert first["rows_upserted"] >= 1
    assert second["rows_upserted"] >= 1


@pytest.mark.unit
def test_many_messages_open_one_pool_on_one_loop(
    writer: ConsumerFlowProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-17427 (C28): N heartbeats open one asyncpg pool, not N.

    The real ``AsyncpgAdapter`` is driven with ``asyncpg.create_pool`` counted,
    so the count is of the pools the production adapter would dial. The
    statement path still runs through the loop-affinity check above, so a pool
    reused on a different loop would raise instead of counting as one.
    """
    import omnimarket.adapters.asyncpg_adapter as adapter_module
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    created: list[_LoopBoundPool] = []
    recording = _RecordingAdapter()

    async def _counting_create_pool(dsn: str, **_: object) -> _LoopBoundPool:
        pool = _LoopBoundPool()
        created.append(pool)
        recording._pool = pool
        return pool

    monkeypatch.setattr(adapter_module.asyncpg, "create_pool", _counting_create_pool)
    real = AsyncpgAdapter(dsn="postgresql://fixture/db")
    monkeypatch.setattr(real, "execute", recording.execute)
    writer._db = real

    results = [writer.handle(_heartbeat(sequence)) for sequence in range(1, 21)]

    assert all(result["rows_upserted"] == 1 for result in results)
    assert len(created) == 1, (
        f"{len(created)} pools for 20 heartbeats; the writer must open its pool "
        "once and keep it for its life"
    )
    assert real.is_connected
    assert not created[0].closed


@pytest.mark.unit
def test_concurrent_dispatch_threads_share_one_loop_and_one_pool(
    writer: ConsumerFlowProjectionWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime calls ``handle()`` from up to 8 worker threads at once.

    Every call must land on the one dispatch loop, and the first connect must
    not race a second one into existence.
    """
    loops: set[int] = set()
    original = writer._project_one_message

    async def _recording(*args: Any) -> dict[str, Any]:
        loops.add(id(asyncio.get_running_loop()))
        return await original(*args)

    monkeypatch.setattr(writer, "_project_one_message", _recording)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(writer.handle, [_heartbeat(n) for n in range(1, 33)]))

    assert all(result["rows_upserted"] == 1 for result in results)
    assert len(loops) == 1
    assert cast("_RecordingAdapter", writer.db).connects == 1
    assert writer._dispatch_thread is not threading.current_thread()


@pytest.mark.unit
def test_a_heartbeat_is_written_in_a_bounded_number_of_statements(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """OMN-17427 (C28): statements per heartbeat do not grow with its deltas.

    One live heartbeat carried 477 consumer deltas; one statement per delta was
    about 5,000 round trips a heartbeat.
    """
    payload = _heartbeat(1)
    template = payload["flow_window"]["consumer_deltas"][0]
    payload["flow_window"]["consumer_deltas"] = [
        {**template, "consumer_group": f"{_STALLED_GROUP}.{index}"}
        for index in range(477)
    ]

    writer.handle(payload)

    statements = [query for query, _ in cast("_RecordingAdapter", writer.db).rows]
    assert len(statements) <= 5, statements


@pytest.mark.unit
def test_applied_payload_carries_the_window_facts_not_a_bare_ack(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """OMN-16875 AC1: the returned result IS the applied event's payload.

    ``handler_wiring`` publishes this dict onto the applied topic, so the facts
    the downstream stall alert needs must be present here. A bare
    ``{"projected": True}`` discards them at the producer, where no consumer can
    reconstruct them.
    """
    result = writer.handle(_heartbeat(1))

    assert result["rows_upserted"] == 1
    rows = result["flow_rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    for field in (
        "consumer_group",
        "topic",
        "window_start",
        "window_end",
        "messages_in",
        "messages_out",
        "messages_dlq",
        "flow_state",
    ):
        assert field in row, f"applied payload dropped {field!r}"
    assert row["consumer_group"] == _STALLED_GROUP
    assert row["messages_in"] == 229150
    assert row["messages_out"] == 0
    assert row["flow_state"] == "STALLED"


@pytest.mark.unit
def test_a_heartbeat_with_no_window_reports_zero_rows(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """A priming tick carries no window: nothing is written, and it says so.

    This path used to return ``True``, which ``_extract_rows_upserted`` mapped to
    one row and the runtime published as a successful projection. Three such
    acks on the dev lane were read as "the first message of each boot
    succeeded"; no message had ever written anything.
    """
    result = writer.handle({"_topic": _IN_TOPIC})

    assert result["rows_upserted"] == 0
    assert result["flow_rows"] == []
