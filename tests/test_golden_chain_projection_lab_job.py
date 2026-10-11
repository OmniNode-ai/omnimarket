# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20604 golden chain: the contract, the two projection traps, the writer.

Rule 7a names two ways to wire a projection wrong, and both have the same
symptom: every message consumed, offsets committed, the table at zero rows and
nothing raised. Each has a test here that fails when the trap is sprung.

  * Trap one: a pure entry on the projection arm. The fold returns rows and
    persists none, so a runtime that dispatches it alone writes nothing.
  * Trap two: a runner-shaped writer that does not declare in-process
    dispatch. The shared runtime classifies it as a standalone runner and
    never dispatches it, and this node has no standalone Deployment.

The trap-two test asks the runtime's own predicate, not a restatement of it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from omnibase_core.constants.constants_runtime_profiles import (
    CONSUMER_ATTACHED_RUNTIME_PROFILES,
)
from omnibase_core.models.errors.model_onex_error import ModelOnexError
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _extract_rows_refused,
    _extract_rows_upserted,
    _is_standalone_projection_runner,
    _prepare_contract_wiring,
)
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    filter_manifest_for_runtime_profile,
)
from pydantic import ValidationError

from omnimarket.events.topics import LAB_JOB_TRANSITIONED_TOPIC_V1
from omnimarket.nodes.node_projection_lab_job.handlers import (
    HandlerProjectionLabJob,
    LabJobProjectionWriter,
)
from omnimarket.nodes.node_projection_lab_job.models import ModelLabJobProjectionRequest
from tests.test_lab_job_projection import life, transitioned

pytestmark = pytest.mark.unit

_OUT_TOPIC = "onex.evt.omnimarket.projection-lab-job-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-lab-job-malformed.v1"  # onex-topic-allow: asserted against the contract

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_job"
    / "contract.yaml"
)


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for the asyncpg adapter and enforces its loop affinity."""

    def __init__(
        self,
        *,
        refuse_all: bool = False,
        rendezvous: threading.Barrier | None = None,
    ) -> None:
        self._pool: _LoopBoundPool | None = None
        self.refuse_all = refuse_all
        self.rendezvous = rendezvous
        self.connects = 0
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.dsn = "postgresql://recording/double"

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()
        self.connects += 1
        if self.rendezvous is not None:
            # Hold every concurrent message here until all have connected, so
            # their connect/execute/close interleave the way concurrent
            # worker threads do on the runtime.
            await asyncio.to_thread(self.rendezvous.wait, 5)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.calls.append((query, params))
        if "RETURNING" in query and not self.refuse_all:
            if "lab_job_state" in query:
                return [{"seq": params[5], "projection_cursor": len(self.calls)}]
            return [{"seq": params[1]}]
        return []


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _writer(**kwargs: Any) -> LabJobProjectionWriter:
    """A writer whose every message goes through ONE recording adapter.

    Sequential tests read one call log; the concurrency test below builds its
    own writer, with one adapter per message as the runtime path does.
    """
    instance = LabJobProjectionWriter()
    adapter = _RecordingAdapter(**kwargs)
    vars(instance)["_db"] = adapter
    vars(instance)["_adapter_for_one_message"] = lambda: adapter
    return instance


def _adapter(writer: LabJobProjectionWriter) -> _RecordingAdapter:
    adapter = writer.db
    assert isinstance(adapter, _RecordingAdapter)
    return adapter


# --------------------------------------------------------------------------
# The declared chain.
# --------------------------------------------------------------------------


def test_the_contract_subscribes_to_exactly_the_registered_topic() -> None:
    contract = _load_contract()
    assert contract["event_bus"]["subscribe_topics"] == [LAB_JOB_TRANSITIONED_TOPIC_V1]
    assert _writer().subscribe_topics == [LAB_JOB_TRANSITIONED_TOPIC_V1]
    assert contract["externally_produced_topics"] == [
        {
            "topic": LAB_JOB_TRANSITIONED_TOPIC_V1,
            "producer": "node_lab_job_orchestrator (plan step M3, not yet built), publishing each reducer transition with its CAS-assigned seq -- not yet a node contract",
        }
    ]


def test_the_contract_declares_the_applied_and_dlq_topics() -> None:
    bus = _load_contract()["event_bus"]
    assert bus["publish_topics"] == [_OUT_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert _load_contract()["terminal_event"] == _OUT_TOPIC
    assert _load_contract()["externally_consumed_topics"] == [_OUT_TOPIC]


def test_the_contract_declares_both_tables_read_write() -> None:
    """Without the table block the consumer advances offsets and never folds."""
    tables = {t["name"]: t for t in _load_contract()["db_io"]["db_tables"]}
    assert set(tables) == {"lab_job_state", "lab_job_transitions"}
    for table in tables.values():
        assert table["schema"] == "omninode_internal"
        assert table["access"] == "read_write"


def test_the_contract_declares_one_ordering_authority() -> None:
    db_io = _load_contract()["db_io"]
    assert db_io["ordering_key"] == "seq"
    assert db_io["dedupe_key"] == ["job_id"]


def test_the_contract_routes_only_the_writer() -> None:
    """OMN-20604 / OMN-19721: a routed pure fold is handed the raw event.

    The request wraps the event under its kind, and only the writer's
    ``build_request`` builds that wrapper. Routing the fold beside the writer
    would fail validation and count zero upserts against
    ``projection_apply_divergence`` (the PR landing template's measured trap).
    """
    handlers = _load_contract()["handler_routing"]["handlers"]
    by_operation = {entry["operation"]: entry["handler"]["name"] for entry in handlers}
    assert by_operation == {
        "lab_job_projection_writer": "LabJobProjectionWriter",
    }


def test_the_fold_refuses_the_raw_event_the_runtime_would_hand_it() -> None:
    """Why the fold cannot be routed: the bare event is not its request."""
    event = transitioned(9)
    raw = {k: v for k, v in event.items() if not k.startswith("_")}
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelLabJobProjectionRequest.model_validate(raw)


# --------------------------------------------------------------------------
# AC2: the two traps.
# --------------------------------------------------------------------------


def test_trap_a_pure_entry_on_the_projection_arm_writes_no_rows() -> None:
    """The fold derives rows and persists none; only the writer writes.

    If the runtime dispatched the fold alone, the event would validate, the
    result would be published, the offset would commit and no table would
    change. So the fold must hold no database and must not claim the
    in-process capability, and the writer, given the same event, must write.
    """
    event = transitioned(9)
    fold = HandlerProjectionLabJob()
    request = LabJobProjectionWriter.build_request(str(event["_topic"]), event)
    result = fold.handle(request)

    assert result.transition_row is not None, "the fold does derive the rows"
    assert not hasattr(fold, "db"), "the fold owns no database, so it writes nothing"
    assert not getattr(fold, "onex_runtime_inprocess_dispatch", False)

    writer = _writer()
    written = writer.handle(dict(event))
    assert written["rows_upserted"] == 2
    statements = [query for query, _ in _adapter(writer).calls]
    assert any(
        "INSERT INTO omninode_internal.lab_job_transitions" in q for q in statements
    )
    assert any("INSERT INTO omninode_internal.lab_job_state" in q for q in statements)


def test_trap_a_writer_without_in_process_dispatch_is_not_dispatched() -> None:
    """The runtime's own predicate: undeclared means standalone, dispatched by nobody."""
    assert LabJobProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert _is_standalone_projection_runner(LabJobProjectionWriter()) is False

    class _Undeclared(LabJobProjectionWriter):
        onex_runtime_inprocess_dispatch = False

    assert _is_standalone_projection_runner(_Undeclared()) is True, (
        "positive control: the same writer without the declaration is skipped"
    )


def test_the_in_process_capability_sits_on_the_writer_operation_only() -> None:
    """On the fold's operation it would dispatch the node twice."""
    for entry in _load_contract()["handler_routing"]["handlers"]:
        klass = (
            LabJobProjectionWriter
            if entry["handler"]["name"] == "LabJobProjectionWriter"
            else HandlerProjectionLabJob
        )
        declared = bool(getattr(klass, "onex_runtime_inprocess_dispatch", False))
        assert declared is entry["operation"].endswith("_projection_writer")


def test_the_writer_calls_the_fold_rather_than_deriving_its_own_rows() -> None:
    assert isinstance(LabJobProjectionWriter()._derive, HandlerProjectionLabJob)


# --------------------------------------------------------------------------
# The writer, against a loop-affine recording double.
# --------------------------------------------------------------------------


def test_the_writer_entry_returns_a_row_count() -> None:
    result = _writer().handle(transitioned(11))
    assert result["rows_upserted"] == 2
    assert result["event_kind"] == "transitioned"
    assert result["state_write_refused"] is False


def test_every_event_of_a_job_life_writes_through_one_loop_each() -> None:
    """One event loop per message: nothing loop-bound survives across calls."""
    writer = _writer()
    events = life()
    for event in events:
        assert writer.handle(dict(event))["rows_upserted"] >= 1
    assert _adapter(writer).connects == len(events)


def test_a_refused_write_is_not_counted() -> None:
    writer = _writer(refuse_all=True)
    result = writer.handle(transitioned(3))
    assert result["rows_upserted"] == 0
    assert result["state_write_refused"] is True
    assert len(_adapter(writer).calls) == 2, "both statements were attempted"


def test_the_runtime_reads_the_writers_count_as_written() -> None:
    """OMN-20604: the count sits under the key the runtime actually reads.

    The runtime's write-path guard and apply counters read ``rows_upserted``
    (omnibase_infra ``_extract_rows_upserted``) and read any other key as 0.
    The template's ``rows_written`` regression let rows land while the runtime
    counted zero upserts and went DEGRADED on ``projection_apply_divergence``.
    This writer must report under the runtime's count key from the outset.
    """
    written = _writer().handle(transitioned(9))
    assert _extract_rows_upserted(written) == 2
    assert _extract_rows_refused(written) == 0


def test_the_runtime_reads_a_guard_refusal_as_a_refusal() -> None:
    refused = _writer(refuse_all=True).handle(transitioned(3))
    assert _extract_rows_upserted(refused) == 0
    assert _extract_rows_refused(refused) == 2


def test_two_messages_in_flight_at_once_do_not_share_a_pool() -> None:
    rendezvous = threading.Barrier(2)
    writer = LabJobProjectionWriter()
    shared = _RecordingAdapter(rendezvous=rendezvous)
    vars(writer)["_db"] = shared
    opened: list[_RecordingAdapter] = []

    def one_per_message() -> _RecordingAdapter:
        adapter = _RecordingAdapter(rendezvous=rendezvous)
        opened.append(adapter)
        return adapter

    vars(writer)["_adapter_for_one_message"] = one_per_message
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def dispatch(event: dict[str, Any]) -> None:
        try:
            results.append(writer.handle(event))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=dispatch, args=(transitioned(seq),)) for seq in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert len(opened) == 2
    assert shared.connects == 0
    assert [r["rows_upserted"] for r in results] == [2, 2]


def test_the_message_adapter_dials_the_dsn_the_runtime_bound() -> None:
    """The per-message adapter inherits the workload DSN, not a default."""
    writer = LabJobProjectionWriter()
    writer.bind_projection_database_url("postgresql://runtime-bound/app")
    adapter = writer._adapter_for_one_message()
    assert adapter.dsn == "postgresql://runtime-bound/app"
    assert adapter is not writer.db
    assert not adapter.is_connected


def test_the_runtime_discovers_the_node_and_prepares_only_the_writer() -> None:
    paths = sorted(_CONTRACT.parent.parent.glob("*/contract.yaml"))
    assert len(paths) > 100
    manifest = discover_contracts_from_paths(paths)
    assert not [e for e in manifest.errors if "lab_job" in str(e)]
    owners = 0
    for profile in sorted(CONSUMER_ATTACHED_RUNTIME_PROFILES):
        owned = filter_manifest_for_runtime_profile(manifest, profile).manifest
        for contract in owned.contracts:
            if contract.name != "projection_lab_job":
                continue
            owners += 1
            with pytest.raises(
                ModelOnexError, match="handler=LabJobProjectionWriter"
            ) as err:
                _prepare_contract_wiring(
                    contract=contract,
                    dispatch_engine=object(),
                    resolver=cast("Any", None),
                    ownership_query=object(),
                    event_bus=None,
                    environment="dev",
                )
            assert "HandlerProjectionLabJob" not in str(err.value)
    assert owners > 0
