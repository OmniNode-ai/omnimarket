# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19833 golden chain: the contract, the two projection traps, the writer.

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
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_projection_pr_landing.handlers import (
    HandlerProjectionPrLanding,
    PrLandingProjectionWriter,
)
from omnimarket.nodes.node_projection_pr_landing.handlers.handler_pr_landing_writer import (
    TOPIC_EVENT_KIND,
)
from omnimarket.nodes.node_projection_pr_landing.models import (
    ModelPrLandingProjectionRequest,
)
from tests.pr_landing_projection_events import (
    T0,
    S,
    a_reopened_pr_life,
    agent_needed,
    merged,
    transitioned,
)

pytestmark = pytest.mark.unit

_OUT_TOPIC = "onex.evt.omnimarket.projection-pr-landing-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-pr-landing-malformed.v1"  # onex-topic-allow: asserted against the contract

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_pr_landing"
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
            # their connect/execute/close interleave the way two topics'
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
            return [{"seq": params[2], "projection_cursor": len(self.calls)}]
        return []


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _writer(**kwargs: Any) -> PrLandingProjectionWriter:
    """A writer whose every message goes through ONE recording adapter.

    Sequential tests read one call log; the concurrency test below builds its
    own writer, with one adapter per message as the runtime path does.
    """
    instance = PrLandingProjectionWriter()
    adapter = _RecordingAdapter(**kwargs)
    instance._db = adapter  # type: ignore[assignment]
    instance._adapter_for_one_message = lambda: adapter  # type: ignore[method-assign]
    return instance


def _adapter(writer: PrLandingProjectionWriter) -> _RecordingAdapter:
    adapter = writer.db
    assert isinstance(adapter, _RecordingAdapter)
    return adapter


# --------------------------------------------------------------------------
# The declared chain.
# --------------------------------------------------------------------------


def _publishers_of(topic: str) -> list[str]:
    """Every omnimarket node contract that declares it publishes ``topic``."""
    publishers: list[str] = []
    for path in sorted(_CONTRACT.parent.parent.glob("*/contract.yaml")):
        if topic not in path.read_text(encoding="utf-8"):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        bus = raw.get("event_bus") or {}
        declared = list(bus.get("publish_topics") or [])
        declared += [e.get("topic") for e in raw.get("published_events") or []]
        if topic in declared:
            publishers.append(path.parent.name)
    return publishers


def test_the_consumer_declaration_lands_with_its_publisher() -> None:
    """All four subscriptions exactly when the orchestrator publishes all four.

    Before the orchestrator handler (OMN-19829) declares its publications,
    this node subscribes to nothing, because the HARD graph gate refuses a
    consumer with no producer. From the commit that adds the publisher, the
    four subscriptions must be present, or the events are published to nobody.
    Either half without the other fails here.
    """
    owned = set(PR_LANDING_EVENT_TOPICS.values())
    published = {topic for topic in owned if _publishers_of(topic)}
    subscribed = set(_load_contract()["event_bus"]["subscribe_topics"])
    assert published in (set(), owned), f"a partial publisher: {sorted(published)}"
    assert subscribed == published
    assert set(_writer().subscribe_topics) == subscribed


def test_the_writer_maps_every_topic_it_will_subscribe() -> None:
    assert set(TOPIC_EVENT_KIND) == set(PR_LANDING_EVENT_TOPICS.values())


def test_the_contract_declares_the_applied_and_dlq_topics() -> None:
    bus = _load_contract()["event_bus"]
    assert bus["publish_topics"] == [_OUT_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert _load_contract()["terminal_event"] == _OUT_TOPIC
    assert _load_contract()["externally_consumed_topics"] == [_OUT_TOPIC]


def test_the_contract_declares_both_tables_read_write() -> None:
    """Without the table block the consumer advances offsets and never folds."""
    tables = {t["name"]: t for t in _load_contract()["db_io"]["db_tables"]}
    assert set(tables) == {"pr_landing_state", "pr_landing_transitions"}
    for table in tables.values():
        assert table["schema"] == "omninode_internal"
        assert table["access"] == "read_write"


def test_the_contract_declares_one_ordering_authority() -> None:
    db_io = _load_contract()["db_io"]
    assert db_io["ordering_key"] == "seq"
    assert db_io["dedupe_key"] == ["repository", "pr_number"]


def test_the_contract_routes_only_the_writer() -> None:
    """OMN-19833 / OMN-19721: a routed pure fold is handed the raw event.

    On the dev lane (2026-09-30T03:27Z) the routed fold failed every message
    with nine ``extra_forbidden`` errors, because its request wraps the event
    under its kind and only the writer's ``build_request`` builds that wrapper,
    and its zero upserts counted against ``projection_apply_divergence``.
    """
    handlers = _load_contract()["handler_routing"]["handlers"]
    by_operation = {entry["operation"]: entry["handler"]["name"] for entry in handlers}
    assert by_operation == {
        "pr_landing_projection_writer": "PrLandingProjectionWriter",
    }


def test_the_fold_refuses_the_raw_event_the_runtime_would_hand_it() -> None:
    """Why the fold cannot be routed: the bare event is not its request."""
    event = transitioned(9, S.CHECKS_PENDING, S.READY, "verdict_green", arm=True)
    raw = {k: v for k, v in event.items() if not k.startswith("_")}
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelPrLandingProjectionRequest.model_validate(raw)


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
    event = transitioned(9, S.CHECKS_PENDING, S.READY, "verdict_green", arm=True)
    fold = HandlerProjectionPrLanding()
    request = PrLandingProjectionWriter.build_request(str(event["_topic"]), event)
    result = fold.handle(request)

    assert result.transition_row is not None, "the fold does derive the rows"
    assert not hasattr(fold, "db"), "the fold owns no database, so it writes nothing"
    assert not getattr(fold, "onex_runtime_inprocess_dispatch", False)

    writer = _writer()
    written = writer.handle(dict(event))
    assert written["rows_upserted"] == 2
    statements = [query for query, _ in _adapter(writer).calls]
    assert any(
        "INSERT INTO omninode_internal.pr_landing_transitions" in q for q in statements
    )
    assert any(
        "INSERT INTO omninode_internal.pr_landing_state" in q for q in statements
    )


def test_trap_a_writer_without_in_process_dispatch_is_not_dispatched() -> None:
    """The runtime's own predicate: undeclared means standalone, dispatched by nobody."""
    assert PrLandingProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert _is_standalone_projection_runner(PrLandingProjectionWriter()) is False

    class _Undeclared(PrLandingProjectionWriter):
        onex_runtime_inprocess_dispatch = False

    assert _is_standalone_projection_runner(_Undeclared()) is True, (
        "positive control: the same writer without the declaration is skipped"
    )


def test_the_in_process_capability_sits_on_the_writer_operation_only() -> None:
    """On the fold's operation it would dispatch the node twice."""
    for entry in _load_contract()["handler_routing"]["handlers"]:
        klass = (
            PrLandingProjectionWriter
            if entry["handler"]["name"] == "PrLandingProjectionWriter"
            else HandlerProjectionPrLanding
        )
        declared = bool(getattr(klass, "onex_runtime_inprocess_dispatch", False))
        assert declared is entry["operation"].endswith("_projection_writer")


def test_the_writer_calls_the_fold_rather_than_deriving_its_own_rows() -> None:
    assert isinstance(PrLandingProjectionWriter()._derive, HandlerProjectionPrLanding)


# --------------------------------------------------------------------------
# The writer, against a loop-affine recording double.
# --------------------------------------------------------------------------


def test_the_writer_entry_returns_a_row_count() -> None:
    result = _writer().handle(merged(11, 1))
    assert result["rows_upserted"] == 1
    assert result["event_kind"] == "merged"
    assert result["state_write_refused"] is False


def test_every_event_of_a_pr_life_writes_through_one_loop_each() -> None:
    """One event loop per message: nothing loop-bound survives across calls."""
    writer = _writer()
    life = a_reopened_pr_life()
    for event in life:
        assert writer.handle(dict(event))["rows_upserted"] >= 1
    assert _adapter(writer).connects == len(life)


def test_a_refused_write_is_not_counted() -> None:
    writer = _writer(refuse_all=True)
    result = writer.handle(transitioned(3, S.OBSERVED, S.PARKED, "evaluated_parked"))
    assert result["rows_upserted"] == 0
    assert result["state_write_refused"] is True
    assert len(_adapter(writer).calls) == 2, "both statements were attempted"


def test_the_runtime_reads_the_writers_count_as_written() -> None:
    """OMN-19833: the count sits under the key the runtime actually reads.

    The runtime's write-path guard and apply counters read ``rows_upserted``
    (omnibase_infra ``_extract_rows_upserted``) and read any other key as 0.
    Reported as ``rows_written``, 9 state rows and 34 transition rows landed on
    the dev lane while the runtime counted zero upserts and went DEGRADED on
    ``projection_apply_divergence``.
    """
    written = _writer().handle(
        transitioned(9, S.CHECKS_PENDING, S.READY, "verdict_green", arm=True)
    )
    assert _extract_rows_upserted(written) == 2
    assert _extract_rows_refused(written) == 0


def test_the_runtime_reads_a_guard_refusal_as_a_refusal() -> None:
    """A redelivery refused by both tables is a refusal, not a silent zero."""
    refused = _writer(refuse_all=True).handle(
        transitioned(3, S.OBSERVED, S.PARKED, "evaluated_parked")
    )
    assert _extract_rows_upserted(refused) == 0
    assert _extract_rows_refused(refused) == 2
    terminal = _writer(refuse_all=True).handle(merged(11, 1))
    assert _extract_rows_refused(terminal) == 1, "a terminal attempts one statement"


def test_two_topics_in_flight_at_once_do_not_share_a_pool() -> None:
    """OMN-19833: the runtime runs this ONE instance from several threads.

    It is routed on all four topics and dispatched through ``asyncio.to_thread``,
    so a transitioned and an agent-needed event of the same transition are in
    ``handle()`` together. With the pool on the shared adapter, one thread's
    loop used and closed the pool the other opened
    (``PoolConnectionHolder.wait_until_released`` on the dev lane). Each
    message must open its own adapter on its own loop.
    """
    rendezvous = threading.Barrier(2)
    writer = PrLandingProjectionWriter()
    shared = _RecordingAdapter(rendezvous=rendezvous)
    writer._db = shared  # type: ignore[assignment]
    opened: list[_RecordingAdapter] = []

    def _one_per_message() -> _RecordingAdapter:
        adapter = _RecordingAdapter(rendezvous=rendezvous)
        opened.append(adapter)
        return adapter

    writer._adapter_for_one_message = _one_per_message  # type: ignore[assignment, method-assign]

    events = [
        transitioned(4, S.CHECKS_PENDING, S.NEEDS_AGENT, "verdict_red"),
        agent_needed(4),
    ]
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def _dispatch(event: dict[str, Any]) -> None:
        try:
            results.append(writer.handle(dict(event)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_dispatch, args=(e,)) for e in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == [], f"a message used another loop's pool: {errors!r}"
    assert len(opened) == 2, "one adapter per message"
    assert shared.connects == 0, "the shared adapter never opens a pool"
    assert sorted(r["rows_upserted"] for r in results) == [1, 2]


def test_the_message_adapter_dials_the_dsn_the_runtime_bound() -> None:
    """The per-message adapter inherits the workload DSN, not a default."""
    writer = PrLandingProjectionWriter()
    writer.bind_projection_database_url("postgresql://runtime-bound/app")
    adapter = writer._adapter_for_one_message()
    assert adapter.dsn == "postgresql://runtime-bound/app"
    assert adapter is not writer.db
    assert not adapter.is_connected


def test_only_a_transition_appends_to_the_log() -> None:
    writer = _writer()
    writer.handle(agent_needed(6))
    writer.handle(merged(11, 1))
    statements = [query for query, _ in _adapter(writer).calls]
    assert not any("pr_landing_transitions" in q for q in statements)


def test_the_upsert_carries_the_stale_write_guard_in_sql() -> None:
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed"))
    upsert = next(
        q
        for q, _ in _adapter(writer).calls
        if "ON CONFLICT (repository, pr_number)" in q
    )
    assert "WHERE omninode_internal.pr_landing_state.seq < EXCLUDED.seq" in upsert
    assert "RETURNING" in upsert


def test_the_log_is_appended_and_never_updated() -> None:
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed"))
    append = next(q for q, _ in _adapter(writer).calls if "pr_landing_transitions" in q)
    assert "ON CONFLICT (repository, pr_number, seq) DO NOTHING" in append
    assert "UPDATE" not in append


def test_the_writer_binds_the_event_time_not_a_string() -> None:
    """The double accepts any type; the real-Postgres module proves the column."""
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed", time=T0))
    _, params = next(
        (q, p) for q, p in _adapter(writer).calls if "pr_landing_transitions" in q
    )
    assert params[-1] == T0


# --------------------------------------------------------------------------
# The runtime's own discovery and wiring, run over the real node tree.
# --------------------------------------------------------------------------


def _prepare(contract: Any) -> Any:
    return _prepare_contract_wiring(
        contract=contract,
        dispatch_engine=object(),
        resolver=cast("Any", None),
        ownership_query=object(),
        event_bus=None,
        environment="dev",
    )


def test_the_runtime_discovers_the_node_and_wires_exactly_what_it_declares(
    tmp_path: Path,
) -> None:
    """Discovery accepts the contract; wiring subscribes to nothing it withholds.

    While the consumer declaration waits for its publisher, every consumer
    profile that owns the node skips it for having no subscriptions, so the
    node cannot consume offsets it would not fold. The positive control adds
    the four subscriptions to a copy and shows the same code then goes on to
    prepare the handlers, so the skip comes from the withheld declaration and
    not from a contract the runtime cannot read.
    """
    paths = sorted(_CONTRACT.parent.parent.glob("*/contract.yaml"))
    assert len(paths) > 100, "the scan must cover the whole node tree"
    manifest = discover_contracts_from_paths(paths)
    assert not [e for e in manifest.errors if "pr_landing" in str(e)]
    subscribed = _load_contract()["event_bus"]["subscribe_topics"]

    owners = 0
    for profile in sorted(CONSUMER_ATTACHED_RUNTIME_PROFILES):
        owned = filter_manifest_for_runtime_profile(manifest, profile).manifest
        for contract in owned.contracts:
            if contract.name != "projection_pr_landing":
                continue
            owners += 1
            if subscribed:
                continue
            prepared = _prepare(contract)
            assert prepared.subscription_topics == []
            assert prepared.prepared_wirings == []
            assert prepared.skip_result is not None
            assert prepared.skip_result.outcome is EnumWiringOutcome.SKIPPED
    assert owners > 0, "no consumer profile owns the node"

    raw = _load_contract()
    raw["event_bus"]["subscribe_topics"] = sorted(PR_LANDING_EVENT_TOPICS.values())
    staged = tmp_path / "omnimarket" / "nodes" / "node_projection_pr_landing"
    staged.mkdir(parents=True)
    (staged / "contract.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (wired,) = discover_contracts_from_paths([staged / "contract.yaml"]).contracts
    # The runtime goes on to prepare the handlers (the null resolver in
    # _prepare stops it there), and the ONLY handler it prepares is the writer:
    # the fold is not routed (OMN-19833).
    with pytest.raises(
        ModelOnexError, match="handler=PrLandingProjectionWriter"
    ) as err:
        _prepare(wired)
    assert "HandlerProjectionPrLanding" not in str(err.value)
