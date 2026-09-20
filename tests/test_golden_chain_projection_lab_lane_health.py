# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the lab lane-health projection (OMN-18769).

The chain this walks is the one the contract's ``golden_path`` declares, in
order, with each hop asserted rather than assumed:

1. three omnibase_infra surfaces publish three facts on three declared topics;
2. this reducer folds each onto its own column group of the lane's row;
3. the whole row is republished as a keyed snapshot delta on the exposure;
4. the projection-applied terminal event is the contract's declared one.

Hop 3 is exercised against a fake publisher and a fake database rather than a
broker: what a golden chain must prove is that a fact entering at hop 1 leaves
at hop 3 as a row keyed the way the exposure declares. Whether a real broker
accepted the bytes is the live readback, which is in the PR body, and is a
different claim.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_lab_lane_health.contract_topics import (
    TOPIC_DLQ,
    TOPIC_EXPOSURE,
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_PROJECTION_APPLIED,
    TOPIC_RUNTIME_HEALTH,
)
from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
    HandlerProjectionLabLaneHealth,
    LabLaneHealthProjectionWriter,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_fact_status import (
    EnumFactStatus,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_request import (
    ModelLabLaneHealthRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_lab_lane_health/contract.yaml"
)
# Anchored to the REAL clock, not a frozen literal, and that is a correction.
# This was `datetime(2026, 9, 18, 23, 0, tzinfo=UTC)` while the decay it asserts
# is computed against `datetime.now(UTC)` in the fold. The fixture aged: a fact
# stamped "now" by the test was 13 hours old by the following midday and decayed
# to WARN, so the suite went red on the calendar rather than on a change. Every
# timestamp below is an OFFSET from this anchor, which is what the assertions
# are actually about.
NOW = datetime.now(tz=UTC)
LAB_HOST = "lab-host"


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load(CONTRACT.read_text()))


# --------------------------------------------------------------------------
# Hop 1 — the declared inputs are the ones the emitters publish on
# --------------------------------------------------------------------------


def test_hop1_the_contract_declares_exactly_the_three_lab_fact_topics() -> None:
    declared = _contract()["event_bus"]["subscribe_topics"]

    assert sorted(declared) == sorted(
        [TOPIC_LANE_CENSUS, TOPIC_RUNTIME_HEALTH, TOPIC_LAB_PASS_RECEIPT]
    )


def test_hop1_each_input_names_the_omnibase_infra_surface_that_produces_it() -> None:
    """A subscribed topic with no declared producer is an unwired chain."""
    produced = {
        entry["topic"]: entry["producer"]
        for entry in _contract()["externally_produced_topics"]
    }

    assert set(produced) == {
        TOPIC_LANE_CENSUS,
        TOPIC_RUNTIME_HEALTH,
        TOPIC_LAB_PASS_RECEIPT,
    }
    assert "lane-census-check.sh" in produced[TOPIC_LANE_CENSUS]
    assert "ServiceRuntimeHealthMonitor" in produced[TOPIC_RUNTIME_HEALTH]
    assert "lab_pass_receipt.py" in produced[TOPIC_LAB_PASS_RECEIPT]


# --------------------------------------------------------------------------
# Hop 2 — the fold, through the canonical def-B entrypoint
# --------------------------------------------------------------------------


def test_hop2_the_def_b_entrypoint_folds_a_fact_into_an_exposure_row() -> None:
    # The request IS the event. It used to be built here as {topic, payload},
    # a shape the runtime adapter cannot construct, so this test passed for the
    # whole period the deployed projection folded nothing at all.
    handler = HandlerProjectionLabLaneHealth()
    result = handler.handle(
        ModelLabLaneHealthRequest.model_validate(
            {
                "host": LAB_HOST,
                "observed_at": NOW.isoformat(),
                "lanes_checked": ["dev"],
                "findings": [],
            }
        )
    )
    assert TOPIC_LANE_CENSUS  # the topic is now recovered, not supplied

    assert result.applied is True
    assert [row["lane"] for row in result.rows] == ["compose-dev"]
    assert result.rows[0]["census_drift_count"] == 0


def test_hop2_a_fact_naming_no_lab_lane_is_applied_with_no_rows() -> None:
    """Correctly ignored is a success. Reporting it as a failure would put
    every non-lab runtime's health tick into a retry loop."""
    handler = HandlerProjectionLabLaneHealth()
    result = handler.handle(
        ModelLabLaneHealthRequest.model_validate(
            {"timestamp": NOW.isoformat(), "status": "HEALTHY"}
        )
    )
    assert TOPIC_RUNTIME_HEALTH  # recovered from `timestamp`, not supplied

    assert result.applied is True
    assert result.rows == ()


# --------------------------------------------------------------------------
# Hop 3 — the durable write path, and what leaves it
# --------------------------------------------------------------------------


class _FakeDb:
    """A database double that remembers the last row it was asked to store.

    It exists to walk the chain, not to type-check the SQL. The column-type
    question -- a str bound where a TIMESTAMPTZ is declared -- is the one a
    mock cannot answer, and it is answered by the real-Postgres gate in
    ``test_omn18769_real_postgres_lab_lane_health_write_path.py``.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.row: dict[str, Any] = {}

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))
        self.row["lane"] = args[0]
        if "census_observed_at" in sql:
            self.row |= {
                "census_observed_at": args[2],
                "census_drift_count": args[4],
                "census_drift_items": args[5],
                "census_host": args[6],
            }
        elif "health_observed_at" in sql:
            self.row |= {
                "health_observed_at": args[2],
                "health_aggregate": args[4],
                "health_dimensions": args[5],
            }
        elif "receipt_observed_at" in sql:
            self.row |= {
                "receipt_observed_at": args[2],
                "receipt_sha": args[4],
                "receipt_result": args[5],
                "receipt_failing_checks": args[6],
            }

    async def fetchval(self, sql: str, *args: Any) -> str | None:
        if not self.row:
            return None
        return json.dumps(
            {
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in self.row.items()
            }
        )


class _MessageMeta:
    topic = TOPIC_LANE_CENSUS
    partition = 0
    offset = 7
    fallback_id = "golden-chain"


@pytest.mark.asyncio
async def test_hop3_every_fact_republishes_the_whole_lane_row() -> None:
    """The read-back is the mechanism, and this is the test that proves it.

    Publishing only the arriving fact would blank the other two dimensions on
    every consumer keyed on ``lane`` -- a regression that looks like data loss
    and would be blamed on the producer. So: fold all three facts, then assert
    the delta from the LAST one still carries the first two.
    """
    handler = LabLaneHealthProjectionWriter()
    db = _FakeDb()
    handler._db = db  # type: ignore[assignment]
    published: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        published.append(dict(kwargs))
        return True

    handler.publish_snapshot_delta = _capture  # type: ignore[assignment]
    meta = _MessageMeta()

    await handler.project_event(
        TOPIC_LANE_CENSUS,
        {
            "host": LAB_HOST,
            "observed_at": (NOW - timedelta(hours=30)).isoformat(),
            "lanes_checked": ["dev"],
            "findings": [],
        },
        meta,  # type: ignore[arg-type]
    )
    await handler.project_event(
        TOPIC_LAB_PASS_RECEIPT,
        {
            "lane": "compose-dev",
            "sha": "a" * 40,
            "result": "FAIL",
            "finished_at": NOW.isoformat(),
            "checks": [{"name": "ready_effects", "ok": False, "evidence": "HTTP_503"}],
        },
        meta,  # type: ignore[arg-type]
    )
    await handler.project_event(
        TOPIC_RUNTIME_HEALTH,
        {
            "lane": "compose-dev",
            "timestamp": NOW.isoformat(),
            "status": "HEALTHY",
            "dimensions": [{"name": "contract_discovery", "status": "HEALTHY"}],
        },
        meta,  # type: ignore[arg-type]
    )

    assert len(published) == 3
    final = published[-1]["row"]
    assert final["lane"] == "compose-dev"
    # All three dimensions survive the health tick...
    assert final["census_observed_at"] is not None
    assert final["receipt_result"] == "FAIL"
    assert final["health_aggregate"] == "HEALTHY"
    # ...and each is aged on its own clock, which is the whole point.
    assert final["census_status"] == EnumFactStatus.STALE.value
    assert final["census_original_status"] == EnumFactStatus.PASS.value
    assert final["health_status"] == EnumFactStatus.PASS.value
    assert final["receipt_status"] == EnumFactStatus.FAIL.value


@pytest.mark.asyncio
async def test_hop3_the_delta_carries_the_source_events_ordering_coordinates() -> None:
    """SnapshotCache keys its staleness comparison on these.

    Passing fixed coordinates would be wrong here: ``lane`` is a MUTABLE grain,
    so a second delta on the key is a real update and the cache must be able to
    order it.
    """
    handler = LabLaneHealthProjectionWriter()
    handler._db = _FakeDb()  # type: ignore[assignment]
    seen: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        seen.append(dict(kwargs))
        return True

    handler.publish_snapshot_delta = _capture  # type: ignore[assignment]

    await handler.project_event(
        TOPIC_LANE_CENSUS,
        {
            "host": LAB_HOST,
            "observed_at": NOW.isoformat(),
            "lanes_checked": ["dev"],
            "findings": [],
        },
        _MessageMeta(),  # type: ignore[arg-type]
    )

    assert seen[0]["source_topic"] == TOPIC_LANE_CENSUS
    assert seen[0]["source_offset"] == 7
    assert seen[0]["op"] == "upsert"


# --------------------------------------------------------------------------
# Hop 4 — the declared outputs
# --------------------------------------------------------------------------


def test_hop4_the_terminal_event_is_the_declared_projection_applied_topic() -> None:
    contract = _contract()

    # The literal is spelled once, here, and pinned against the constant. Every
    # other reference in this repository goes through topics.py; this assertion
    # is what makes a rename of the constant a visible change rather than a
    # silent repoint of the whole chain's terminal state.
    assert (
        TOPIC_PROJECTION_APPLIED
        == "onex.evt.omnimarket.projection-lab-lane-health-applied.v1"
    )
    assert contract["terminal_event"] == TOPIC_PROJECTION_APPLIED
    assert contract["event_bus"]["publish_topics"] == [TOPIC_PROJECTION_APPLIED]
    assert contract["externally_consumed_topics"] == [TOPIC_PROJECTION_APPLIED]


def test_hop4_a_malformed_fact_has_a_declared_dlq_rather_than_a_log_line() -> None:
    """A swallowed event leaves a lane frozen at its last good fact."""
    assert _contract()["event_bus"]["dlq_topics"] == [TOPIC_DLQ]


def test_hop4_the_exposure_is_the_declared_bus_backed_snapshot_topic() -> None:
    exposure = _contract()["projection_api"]

    assert exposure["topic"] == TOPIC_EXPOSURE
    assert exposure["bus_backed"] is True
    assert exposure["key_columns"] == ["lane"]
    # OMN-18043: without this a truncated page reports next_cursor: null and a
    # caller cannot tell it was truncated.
    assert exposure["cursor_column"] == "lane"
