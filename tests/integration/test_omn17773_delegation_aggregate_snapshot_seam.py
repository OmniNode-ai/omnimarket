# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain seam test for OMN-17773 (G1 delegation aggregate bus-backing).

GOAL row 0 leg (b). The dev-lane projection API's ``GET /morning`` renders
its delegation-savings panel as ``REFUSED: not_yet_bus_backed`` while the
data behind it is live -- measured on the dev lane 2026-09-03T15:2xZ:
``delegation_events`` holds 182 rows (newest ``created_at`` 13:49:25+00) and
``projection_delegation_summary`` returns ``totalDelegations 182`` /
``totalSavingsUsd 10.464755``. The exposure is invisible only because it
declares ``bus_backed: false``, and the projection API holds no DB handle by
design (OMN-15800 seam B): an exposure becomes visible when its writer
republishes it on the topic ``SnapshotCache`` reads.

The four exposures converted here are ``limit: 1`` SQL views over
``delegation_events``, so ``publish_snapshot_delta`` has no per-row upsert to
hook. The runner instead re-reads each declared singleton aggregate after a
successful apply and publishes one snapshot keyed on a query-produced
constant grain -- so each compacted topic holds exactly ONE live key forever,
which is the property OMN-17345 records the other snapshot topics lacking
(``consumer-flow`` keys on ``window_start`` and grows 1,166 keys/minute).

This drives the same three seams ``test_projection_bus_seam.py`` does, with
every production class real:

  Seam A: the REAL ``DelegationProjectionRunner.project_event`` -- the exact
  method the deployed ``omnimarket-projection-delegation-writer`` container
  runs -- processes a real ``delegation-completed.v1``-shaped payload and
  publishes the aggregate snapshots, captured at the AIOKafkaProducer
  boundary so the exact key/value/header bytes a live broker would receive
  are asserted.

  Seam B: those exact bytes are fed to the REAL ``SnapshotCache.apply_message``.

  Seam C: the REAL ``build_savings_panel`` + ``render_page`` (the morning
  page's own code path) renders the cached row, and the assertion is that the
  panel carries the measured number instead of the refusal.

RED before the fix, all four assertions failing for the same reason:
  * ``test_contract_declares_the_four_aggregates_bus_backed`` -- every one of
    the four exposures parsed ``bus_backed=False`` with empty ``key_columns``.
  * ``test_apply_publishes_one_keyed_snapshot_per_aggregate`` --
    ``DelegationProjectionRunner`` had no ``_aggregate_exposures`` attribute
    and published nothing; ``sent`` was empty.
  * ``test_cached_aggregate_row_reaches_the_savings_panel`` -- with nothing
    published there was no row to cache, and ``build_savings_panel`` emitted
    zero metrics.
  * ``test_bus_backed_exposure_without_a_publish_site_is_refused`` -- the
    construction-time guard did not exist.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    SNAPSHOT_GRAIN_COLUMN,
    DelegationProjectionRunner,
)
from omnimarket.projection.morning_page import (
    TOPIC_DELEGATION_SUMMARY,
    EnumPanelState,
    build_savings_panel,
    read_projection,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_cache import SnapshotCache

SUMMARY_TOPIC = "onex.snapshot.projection.delegation.summary.v1"
QUALITY_GATE_TOPIC = "onex.snapshot.projection.delegation.quality-gate.v1"
TOKEN_USAGE_TOPIC = "onex.snapshot.projection.delegation.token-usage.v1"
MODEL_ROUTING_TOPIC = "onex.snapshot.projection.delegation.model-routing.v1"

AGGREGATE_TOPICS = (
    SUMMARY_TOPIC,
    QUALITY_GATE_TOPIC,
    TOKEN_USAGE_TOPIC,
    MODEL_ROUTING_TOPIC,
)

_CORRELATION_ID = "3f1c0d9a-77b2-4a6e-9d1f-0c5b8e2a4417"
_TENANT = "beta-business-proof"

# The exact live values measured on the .201 dev lane at 2026-09-03T15:2xZ via
# `docker exec omnibase-infra-postgres psql -U postgres -d omnidash_analytics`.
# The view returns these columns; this is the real row shape, not an invention.
_LIVE_SUMMARY_ROW: dict[str, Any] = {
    "totalDelegations": 182,
    "qualityGatePassRate": 0.2032967032967033,
    "qualityGatePassed": 37,
    "qualityGateTotal": 182,
    "totalSavingsUsd": 10.464755,
    "avgLatencyMs": 260008.67582417582,
    "latestEventAt": 1788443365.48249,
    "total_events": 182,
    "quality_passed_count": 37,
    "quality_failed_count": 145,
    "avg_latency_ms": 260008.67582417582,
    "latest_event_at": 1788443365.48249,
    "byTaskType": [{"count": 90, "taskType": "test"}],
    "byModel": [{"count": 108, "model": "gemini-2.5-flash"}],
    "latest_projection_updated_at": datetime(2026, 9, 3, 13, 49, 25, tzinfo=UTC),
}

_AGGREGATE_ROWS: dict[str, dict[str, Any]] = {
    "projection_delegation_summary": _LIVE_SUMMARY_ROW,
    "projection_delegation_quality_gate": {
        "overall_pass_rate": 0.2032967032967033,
        "total_passed": 37,
        "total_failed": 145,
        "total_checks": 182,
        "by_check_type": [],
        "provisioned": True,
        "latest_projection_updated_at": _LIVE_SUMMARY_ROW[
            "latest_projection_updated_at"
        ],
    },
    "projection_delegation_token_usage": {
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "by_model": [],
        "provisioned": True,
        "latest_projection_updated_at": _LIVE_SUMMARY_ROW[
            "latest_projection_updated_at"
        ],
    },
    "projection_delegation_model_routing": {
        "total_delegations": 182,
        "rows": [],
        "by_model": [],
        "by_tier": [],
        "provisioned": True,
        "latest_projection_updated_at": _LIVE_SUMMARY_ROW[
            "latest_projection_updated_at"
        ],
    },
}


def _mock_db() -> MagicMock:
    """A DB double that answers the aggregate re-read like real Postgres.

    ``AsyncpgAdapter.execute`` returns ``list[dict]``. The aggregate re-read
    selects a literal grain column alongside ``agg.*``, so the returned dict
    carries that column exactly as Postgres would materialize it; every other
    statement (the evidence-preservation SELECT, the INSERTs) answers with the
    no-rows list the real adapter returns.
    """

    async def _execute(
        query: str, *params: Any, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        for relation, row in _AGGREGATE_ROWS.items():
            if f"FROM {relation} " in query:
                assert params, (
                    "the aggregate re-read must bind its grain as a parameter, "
                    "never interpolate it into the SQL text"
                )
                return [{SNAPSHOT_GRAIN_COLUMN: params[0], **row}]
        return []

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.fetchval = AsyncMock(return_value=None)
    return db


def _fake_producer() -> tuple[MagicMock, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def _send_and_wait(
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        sent.append({"topic": topic, "value": value, "key": key, "headers": headers})

    producer = MagicMock()
    producer.send_and_wait = AsyncMock(side_effect=_send_and_wait)
    return producer, sent


def _delegation_completed_payload() -> dict[str, Any]:
    """The real ``onex.evt.omnibase-infra.delegation-completed.v1`` shape.

    Same field vocabulary as the accepted producer fixture in
    ``tests/test_omn15905_delegation_projection_writer_seam.py``.
    """
    return {
        "correlation_id": _CORRELATION_ID,
        "tenant_id": _TENANT,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
        "context_pack_hash": "sha256:omn17773-seam",
    }


def _run_one_apply() -> tuple[DelegationProjectionRunner, list[dict[str, Any]]]:
    runner = DelegationProjectionRunner()
    runner._db = _mock_db()  # type: ignore[assignment]
    producer, sent = _fake_producer()
    runner._producer = producer

    topic = runner._topic_delegation_completed
    assert topic, "contract must declare a delegation-completed topic"
    meta = MessageMeta(partition=0, offset=4711, fallback_id=str(uuid4()), topic=topic)
    ok = asyncio.run(runner.project_event(topic, _delegation_completed_payload(), meta))
    assert ok is True, "the canonical delegation terminal must apply"
    return runner, sent


@pytest.mark.unit
class TestContractDeclaresTheAggregates:
    def test_contract_declares_the_four_aggregates_bus_backed(self) -> None:
        """AC1: the flag and the key land in the contract, not in code."""
        runner = DelegationProjectionRunner()
        by_topic = {e.topic: e for e in runner._aggregate_exposures}

        assert set(by_topic) == set(AGGREGATE_TOPICS), (
            "exactly the four singleton delegation aggregates are bus-backed; "
            f"got {sorted(by_topic)}"
        )
        for topic, exposure in by_topic.items():
            assert exposure.bus_backed is True, topic
            assert exposure.key_columns == (SNAPSHOT_GRAIN_COLUMN,), topic
            assert exposure.limit == 1, (
                f"{topic} is republished whole on every apply, so it must be "
                "a singleton exposure"
            )

    def test_per_row_delegation_exposures_stay_refused(self) -> None:
        """The multi-tenant per-row exposures must NOT have been flipped.

        ``delegation_events`` carries three distinct ``tenant_id`` values on
        the dev lane (179 / 2 / 1, measured 2026-09-03). ``SnapshotCache.get_rows``
        applies no tenant filter without a declared ``tenant_column``, so
        bus-backing these unscoped reproduces exactly the cross-tenant leak
        ``node_projection_savings/contract.yaml`` refused to ship for
        ``savings.v1``. This test is the guard against a later well-meaning
        sweep flipping them for the sake of the census number.
        """
        contract_path = (
            Path(__file__).resolve().parents[2]
            / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
        )
        with open(contract_path) as handle:
            contract = yaml.safe_load(handle)
        by_topic = {e["topic"]: e for e in contract["projection_api"]["exposures"]}
        for topic in (
            "delegation",
            "onex.snapshot.projection.delegation.decisions.v1",
            "onex.snapshot.projection.delegation.correlation-trace.v1",
            "onex.evt.omnimarket.projection-delegation-events.v1",
        ):
            assert by_topic[topic].get("bus_backed", False) is False, (
                f"{topic} reads multi-tenant delegation_events per row and "
                "must stay SQL-served until it declares a tenant_column"
            )


@pytest.mark.unit
class TestApplyPublishesAggregateSnapshots:
    def test_apply_publishes_one_keyed_snapshot_per_aggregate(self) -> None:
        """Seam A: AC1 -- one keyed delta per aggregate, per apply."""
        _runner, sent = _run_one_apply()

        by_topic: dict[str, list[dict[str, Any]]] = {}
        for message in sent:
            by_topic.setdefault(message["topic"], []).append(message)

        for topic in AGGREGATE_TOPICS:
            assert topic in by_topic, f"no snapshot published for {topic}"
            assert len(by_topic[topic]) == 1, (
                f"{topic} must receive exactly one delta per apply, "
                f"got {len(by_topic[topic])}"
            )
            message = by_topic[topic][0]
            # The compaction key is the constant grain, so the topic holds one
            # live key forever (OMN-17345's property).
            assert message["key"] == topic.encode("utf-8")
            assert message["value"] is not None, "an upsert, not a tombstone"
            header_map = dict(message["headers"])
            assert header_map["schema_version"] == b"projection_snapshot.v1"
            assert header_map["content_type"] == b"application/json"

        summary = json.loads(by_topic[SUMMARY_TOPIC][0]["value"])
        assert summary["op"] == "upsert"
        assert summary["key"] == [SUMMARY_TOPIC]
        # The measured number, carried end to end -- not a placeholder.
        assert summary["row"]["totalDelegations"] == 182
        assert summary["row"]["totalSavingsUsd"] == 10.464755
        # The ordering authority is the SOURCE message's Kafka coordinates.
        assert summary["source_offset"] == 4711
        assert summary["source_partition"] == 0

    def test_key_is_stable_across_applies(self) -> None:
        """Two applies produce the same key, so compaction collapses them.

        This is the difference between this exposure and consumer-flow, whose
        key includes ``window_start`` and therefore mints a new key every
        window (OMN-17345: 9.09M records, log-start still 0).
        """
        _first, sent_a = _run_one_apply()
        _second, sent_b = _run_one_apply()
        keys_a = {m["key"] for m in sent_a if m["topic"] == SUMMARY_TOPIC}
        keys_b = {m["key"] for m in sent_b if m["topic"] == SUMMARY_TOPIC}
        assert keys_a == keys_b == {SUMMARY_TOPIC.encode("utf-8")}


@pytest.mark.unit
class TestCachedAggregateReachesThePage:
    def test_cached_aggregate_row_reaches_the_savings_panel(self) -> None:
        """Seams B+C: AC5 -- the panel carries the number, not the refusal."""
        runner, sent = _run_one_apply()
        exposure = next(
            e for e in runner._aggregate_exposures if e.topic == SUMMARY_TOPIC
        )
        published = next(m for m in sent if m["topic"] == SUMMARY_TOPIC)

        cache = SnapshotCache(
            {SUMMARY_TOPIC: exposure},
            bootstrap_servers="unused:9092",
            group_id="test-omn17773-aggregate-seam",
        )
        cache.apply_message(
            published["topic"],
            published["key"],
            published["value"],
            published["headers"],
        )
        # No live consumer in this test -- bootstrap is marked complete
        # directly, the same way test_projection_bus_seam.py does.
        cache._state[SUMMARY_TOPIC].bootstrap_complete = True

        read = read_projection(
            SUMMARY_TOPIC, {SUMMARY_TOPIC: exposure}, cache, limit=exposure.limit
        )
        assert read.state == EnumPanelState.LIVE, (
            f"expected LIVE, got {read.state} / {read.reason}"
        )

        panel = build_savings_panel((read,))
        rendered = {metric.label: metric.value for metric in panel.metrics}
        assert rendered["delegations recorded"] == "182", rendered
        assert all(
            metric.source_topic == TOPIC_DELEGATION_SUMMARY for metric in panel.metrics
        )


@pytest.mark.unit
class TestFlagCannotOutrunTheWriter:
    def test_bus_backed_exposure_without_a_publish_site_is_refused(
        self, tmp_path: Path
    ) -> None:
        """OMN-15864's ordering rule, enforced in this runner's constructor.

        Flipping ``bus_backed`` on a per-row exposure this runner has no
        publish site for would convert an honest refusal into a confident
        empty page. The runner republishes exactly the exposures keyed on the
        grain column; any other bus-backed exposure must fail construction
        rather than serve nothing.
        """
        source = (
            Path(__file__).resolve().parents[2]
            / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
        )
        with open(source) as handle:
            contract = yaml.safe_load(handle)
        for exposure in contract["projection_api"]["exposures"]:
            if exposure["topic"] == "onex.snapshot.projection.delegation.decisions.v1":
                exposure["bus_backed"] = True
                exposure["key_columns"] = ["correlation_id"]
        forged = tmp_path / "contract.yaml"
        forged.write_text(yaml.safe_dump(contract))

        with pytest.raises(ValueError, match="no publish site"):
            DelegationProjectionRunner(contract_path=forged)
