# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: routing-decision.v1 -> infra_routing_decisions, in the runtime.

One-runtime plan T14. The standalone ``infra_routing_decisions`` consumer
(``omnibase_infra.services.observability.infra_routing_decisions``) is replaced by
this node. ``node_projection_routing_decision`` is NOT that replacement: it
writes ``agent_routing_decisions`` from the omniclaude routing topic, a
different table on a different topic, so it is checked here only to keep that
fact pinned.

What these tests hold:

* the node reads the topic the old consumer read and writes the table it wrote;
* on replayed deliveries the new fold produces the same row the old module's
  own parser produces, and malformed deliveries the old module sent to the DLQ
  are refused here as poison;
* the write is an UPSERT on the deterministic key, so a second replay leaves
  one row per key (the statement text is pinned here; the row count is proven
  against real Postgres in ``test_infra_routing_decisions_real_postgres.py``);
* the node declares the group it takes over from.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _extract_rows_upserted,
    _is_standalone_projection_runner,
)
from omnibase_infra.services.observability.infra_routing_decisions.config import (
    ConfigInfraRoutingDecisionsConsumer,
)
from omnibase_infra.services.observability.infra_routing_decisions.model_routing_decision_ingest import (
    ModelInfraRoutingDecisionIngest,
)

from omnimarket.nodes.node_projection_infra_routing_decisions.handlers import (
    HandlerProjectionInfraRoutingDecisions,
    InfraRoutingDecisionsProjectionWriter,
)
from omnimarket.nodes.node_projection_infra_routing_decisions.models import (
    ModelInfraRoutingDecisionProjectionRequest,
)
from omnimarket.projection.error_classification import PoisonEventError

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_CONTRACT = _NODES / "node_projection_infra_routing_decisions" / "contract.yaml"
_SIBLING_CONTRACT = _NODES / "node_projection_routing_decision" / "contract.yaml"

_TOPIC = "onex.evt.omnibase-infra.routing-decision.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-infra-routing-decisions-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-infra-routing-decisions-malformed.v1"  # onex-topic-allow: asserted against the contract
_OLD_GROUP = "infra-routing-decisions-postgres"

_CORR_A = "11111111-1111-4111-8111-111111111111"
_CORR_B = "22222222-2222-4222-8222-222222222222"
_CORR_ENVELOPE_ONLY = "33333333-3333-4333-8333-333333333333"


def _decision(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "correlation_id": _CORR_A,
        "task_type": "code_generation",
        "selected_model": "qwen3-coder",
        "selected_backend_ref": "lab-qwen",
        "tier_name": "local",
        "rationale": "first healthy local tier",
        "endpoint_url": "http://lab:8000",
        "cost_tier": "free",
    }
    base.update(overrides)
    return base


def _envelope(payload: dict[str, object], **top: object) -> dict[str, object]:
    return {"envelope_id": "e-1", "payload": payload, **top}


#: The replayed deliveries: what the routing reducer publishes, in order, with a
#: redelivery of the first and a decision that changes for the same key.
_REPLAY: list[dict[str, object]] = [
    _envelope(_decision()),
    _envelope(
        _decision(
            correlation_id=_CORR_B,
            selected_model="glm-5.3",
            selected_backend_ref="cloud-glm",
            tier_name="cheap_cloud",
            rationale="local tier saturated",
            task_type=None,
        )
    ),
    _envelope(_decision()),
    _envelope(
        _decision(selected_model="qwen3-coder-next", rationale="reranked"),
    ),
    # correlation id only on the envelope, as the old parser also reads it
    _envelope(
        {k: v for k, v in _decision().items() if k != "correlation_id"},
        correlation_id=_CORR_ENVELOPE_ONLY,
    ),
    # no correlation id anywhere: always inserted, never deduplicated
    _envelope({k: v for k, v in _decision().items() if k != "correlation_id"}),
    # absent wire strings take the NOT NULL '' default
    _envelope({"correlation_id": _CORR_B, "selected_model": "glm-5.3"}),
]

#: Deliveries the old module parsed to ``None`` (its DLQ signal).
_MALFORMED: list[dict[str, object]] = [
    {"envelope_id": "e-2"},
    {"envelope_id": "e-3", "payload": "not-a-dict"},
    _envelope(_decision(correlation_id="not-a-uuid")),
    _envelope(_decision(selected_model="")),
    _envelope({k: v for k, v in _decision().items() if k != "selected_model"}),
]


class _RecordingAdapter:
    """Stands in for the asyncpg adapter; records every statement."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.dsn = "postgresql://recording/double"
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self.connected, "call connect() first"
        self.calls.append((query, params))
        return [{"correlation_id": params[0]}]


def _load_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _writer() -> tuple[InfraRoutingDecisionsProjectionWriter, _RecordingAdapter]:
    instance = InfraRoutingDecisionsProjectionWriter()
    adapter = _RecordingAdapter()
    vars(instance)["_db"] = adapter
    vars(instance)["_adapter_for_one_message"] = lambda: adapter
    return instance, adapter


def _old_row(raw: dict[str, object]) -> dict[str, object] | None:
    parsed = ModelInfraRoutingDecisionIngest.from_envelope(raw)
    return None if parsed is None else parsed.model_dump(mode="python")


def _new_row(raw: dict[str, object]) -> dict[str, object]:
    request = ModelInfraRoutingDecisionProjectionRequest(delivery=raw)
    return (
        HandlerProjectionInfraRoutingDecisions()
        .handle(request)
        .row.model_dump(mode="python")
    )


# --------------------------------------------------------------------------
# AC1: which node owns the table and the topic.
# --------------------------------------------------------------------------


def test_the_existing_routing_decision_node_owns_a_different_table_and_topic() -> None:
    """Pins the T14 first-check: node_projection_routing_decision is not the owner."""
    sibling = yaml.safe_load(_SIBLING_CONTRACT.read_text())
    sibling_tables = {t["name"] for t in sibling["db_io"]["db_tables"]}
    assert sibling_tables == {"agent_routing_decisions"}
    assert _TOPIC not in sibling["event_bus"]["subscribe_topics"]
    assert (
        sibling["event_bus"]["subscribe_topics"]
        == [
            "onex.evt.omniclaude.routing-decision.v1"  # onex-topic-allow: asserted against the sibling contract
        ]
    )


def test_the_new_node_owns_the_table_and_topic_the_old_consumer_used() -> None:
    contract = _load_contract()
    assert contract["event_bus"]["subscribe_topics"] == [_TOPIC]
    default_topics = ConfigInfraRoutingDecisionsConsumer.model_fields["topics"]
    factory = default_topics.default_factory
    assert factory is not None
    assert cast("Callable[[], list[str]]", factory)() == [_TOPIC]
    tables = {t["name"]: t for t in contract["db_io"]["db_tables"]}
    assert set(tables) == {"infra_routing_decisions"}
    table = tables["infra_routing_decisions"]
    # The old consumer dialled the omnibase_infra database, where migration 080
    # created the table. The `application` projection database has no such table.
    assert table["database_ref"] == "omnibase_infra"
    assert table["schema"] == "public"
    assert table["access"] == "write"


def test_the_contract_declares_the_applied_and_dlq_topics() -> None:
    contract = _load_contract()
    assert contract["event_bus"]["publish_topics"] == [_OUT_TOPIC]
    assert contract["event_bus"]["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _OUT_TOPIC
    assert contract["externally_consumed_topics"] == [_OUT_TOPIC]
    assert InfraRoutingDecisionsProjectionWriter().poison_dlq_topics == [_DLQ_TOPIC]


def test_the_contract_routes_only_the_writer() -> None:
    handlers = _load_contract()["handler_routing"]["handlers"]
    by_operation = {entry["operation"]: entry["handler"]["name"] for entry in handlers}
    assert by_operation == {
        "infra_routing_decisions_projection_writer": (
            "InfraRoutingDecisionsProjectionWriter"
        )
    }


# --------------------------------------------------------------------------
# The handover from the old consumer group.
# --------------------------------------------------------------------------


def test_the_handover_names_the_group_the_old_consumer_commits_under() -> None:
    declared = _load_contract()["retired_consumer_group"]
    old_default = ConfigInfraRoutingDecisionsConsumer.model_fields["kafka_group_id"]
    assert old_default.default == _OLD_GROUP
    assert declared["group_id"] == old_default.default
    assert declared["topics"] == [_TOPIC]
    # The old consumer used auto_offset_reset=earliest on a partition it never
    # committed; the successor continues the same way.
    assert (
        declared["from_auto_offset_reset"]
        == ConfigInfraRoutingDecisionsConsumer.model_fields["auto_offset_reset"].default
    )


# --------------------------------------------------------------------------
# AC2: same rows as the module this replaces, on replayed events.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", _REPLAY, ids=lambda r: json.dumps(r, sort_keys=True)[:60]
)
def test_the_fold_yields_the_row_the_old_module_yields(raw: dict[str, object]) -> None:
    expected = _old_row(raw)
    assert expected is not None, "positive control: the old module accepts this"
    assert _new_row(raw) == expected


@pytest.mark.parametrize(
    "raw", _REPLAY, ids=lambda r: json.dumps(r, sort_keys=True)[:60]
)
def test_the_runtime_unwrapped_decision_folds_to_the_same_row(
    raw: dict[str, object],
) -> None:
    """The runtime hands an in-process writer the decision, not the envelope."""
    payload = raw["payload"]
    assert isinstance(payload, dict)
    bare = dict(payload)
    if "correlation_id" not in bare and "correlation_id" in raw:
        bare["correlation_id"] = raw["correlation_id"]
    assert _new_row(bare) == _old_row(raw)


@pytest.mark.parametrize(
    "raw", _MALFORMED, ids=lambda r: json.dumps(r, sort_keys=True)[:60]
)
def test_what_the_old_module_sent_to_the_dlq_is_poison_here(
    raw: dict[str, object],
) -> None:
    assert _old_row(raw) is None, "positive control: the old module refuses this"
    request = ModelInfraRoutingDecisionProjectionRequest(delivery=raw)
    with pytest.raises(PoisonEventError):
        HandlerProjectionInfraRoutingDecisions().handle(request)


def test_replaying_the_same_deliveries_twice_issues_identical_statements() -> None:
    first, first_adapter = _writer()
    second, second_adapter = _writer()
    for raw in _REPLAY:
        first.handle(dict(raw))
    for raw in _REPLAY + _REPLAY:
        second.handle(dict(raw))
    assert len(first_adapter.calls) == len(_REPLAY)
    assert second_adapter.calls == first_adapter.calls + first_adapter.calls


def test_the_write_is_an_upsert_on_the_deterministic_key() -> None:
    writer, adapter = _writer()
    result = writer.handle(dict(_REPLAY[0]))
    ((query, params),) = adapter.calls
    assert "INSERT INTO public.infra_routing_decisions" in query
    # The table's uniqueness is a PARTIAL index; Postgres infers it only when the
    # conflict target repeats the index predicate.
    assert "ON CONFLICT (correlation_id) WHERE correlation_id IS NOT NULL" in query
    assert "DO UPDATE SET" in query
    assert params[0] == UUID(_CORR_A)
    assert _extract_rows_upserted(result) == 1


def test_a_delivery_without_a_key_is_still_inserted() -> None:
    writer, adapter = _writer()
    writer.handle(dict(_REPLAY[5]))
    ((_, params),) = adapter.calls
    assert params[0] is None


def test_the_columns_the_decision_does_not_carry_are_not_written() -> None:
    writer, adapter = _writer()
    writer.handle(dict(_REPLAY[0]))
    ((query, _),) = adapter.calls
    for column in (
        "selection_mode",
        "fallback_indicator",
        "is_fallback",
        "candidates_evaluated",
        "candidate_providers",
        "session_id",
        "latency_ms",
    ):
        assert column not in query


# --------------------------------------------------------------------------
# The two rule-7a traps.
# --------------------------------------------------------------------------


def test_trap_a_pure_entry_on_the_projection_arm_writes_no_rows() -> None:
    fold = HandlerProjectionInfraRoutingDecisions()
    assert not hasattr(fold, "db"), "the fold owns no database, so it writes nothing"
    assert not getattr(fold, "onex_runtime_inprocess_dispatch", False)


def test_trap_a_writer_without_in_process_dispatch_is_not_dispatched() -> None:
    assert InfraRoutingDecisionsProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert (
        _is_standalone_projection_runner(InfraRoutingDecisionsProjectionWriter())
        is False
    )

    class _Undeclared(InfraRoutingDecisionsProjectionWriter):
        onex_runtime_inprocess_dispatch = False

    assert _is_standalone_projection_runner(_Undeclared()) is True, (
        "positive control: the same writer without the declaration is skipped"
    )


def test_one_message_owns_one_adapter_and_loop() -> None:
    writer, adapter = _writer()
    asyncio.run(asyncio.sleep(0))
    writer.handle(dict(_REPLAY[0]))
    assert adapter.connected is False, "the message closed what it opened"
