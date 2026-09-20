"""OMN-18769 — the lab lane-health projection, AC by AC.

Every test here names the acceptance criterion it falsifies. The three that can
only be proven against a live lane (AC1/AC2/AC3's bus readbacks) are proven here
at the SEAM the reducer sees -- the wire shape each emitter promises -- and the
live readback is quoted in the PR body. That split is deliberate and is stated
rather than implied: a unit test cannot prove a message reached a broker, and a
broker readback cannot prove the shape is the one the reducer folds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_projection_lab_lane_health.handlers.lane_health_fold import (
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
    LaneHealthFoldError,
    apply_event,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_fact_status import (
    EnumFactStatus,
    decay,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
    normalize_lane,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_row import (
    ModelLabLaneHealthRow,
)

NOW = datetime(2026, 9, 18, 23, 0, tzinfo=UTC)
# A placeholder, not the lab's address: an internal IP in a test fixture is a
# leaked literal, and the host is not what any assertion here is about.
LAB_HOST = "lab-host"

pytestmark = pytest.mark.unit


def census_event(*, drift: int = 0, at: datetime = NOW) -> dict[str, object]:
    """A census-observed event as the refresh script emits it."""
    findings = [
        {
            "lane": "dev",
            "kind": "unexpected_container",
            "container": f"stray-{index}",
            "detail": "running but not declared",
            "severity": "warning",
        }
        for index in range(drift)
    ]
    return {
        "schema_version": "1.0.0",
        "event_type": "lane-census-observed",
        "topic": TOPIC_LANE_CENSUS,
        "host": LAB_HOST,
        "observed_at": at.isoformat(),
        "lanes_checked": ["dev", "stability-test", "judge"],
        "drift_count": drift,
        "findings": findings,
    }


def health_event(*, status: str = "HEALTHY", at: datetime = NOW) -> dict[str, object]:
    return {
        "lane": "compose-dev",
        "timestamp": at.isoformat(),
        "status": status,
        "dimensions": [
            {
                "name": "contract_discovery",
                "status": "HEALTHY",
                "detail": "412 contracts",
            },
            {"name": "consumer_groups", "status": status, "detail": "9 empty"},
        ],
    }


def receipt_event(
    *, result: str = "PASS", at: datetime = NOW, failing: tuple[str, ...] = ()
) -> dict[str, object]:
    checks = [{"name": "ready_main", "ok": True, "evidence": "HTTP_200"}]
    checks.extend(
        {"name": name, "ok": False, "evidence": "HTTP_503"} for name in failing
    )
    return {
        "lane": "compose-dev",
        "sha": "a" * 40,
        "result": result,
        "finished_at": at.isoformat(),
        "checks": checks,
    }


# --------------------------------------------------------------------------
# AC1 — the census fact reaches the row, clean runs included
# --------------------------------------------------------------------------


def test_ac1_a_clean_census_run_materializes_a_row_with_drift_count_zero() -> None:
    """A no-drift census is a FACT, not an absence.

    The pre-existing drift topic only carries drift, so a reducer on it cannot
    tell "the lane is clean" from "the census has not run for two days". The
    census-observed event carries both, and this is the case that proves it.
    """
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    touched = apply_event(rows, topic=TOPIC_LANE_CENSUS, payload=census_event(drift=0))

    assert [row.lane for row in touched] == [EnumLabLane.COMPOSE_DEV]
    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["census_drift_count"] == 0
    assert wire["census_status"] == EnumFactStatus.PASS.value


def test_ac1_drift_count_on_the_row_is_this_lanes_own_items() -> None:
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(rows, topic=TOPIC_LANE_CENSUS, payload=census_event(drift=5))

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["census_drift_count"] == 5
    assert wire["census_status"] == EnumFactStatus.FAIL.value
    assert len(wire["census_drift_items"]) == 5


# --------------------------------------------------------------------------
# AC2 — the runtime's per-dimension verdicts reach the row
# --------------------------------------------------------------------------


def test_ac2_runtime_health_dimensions_land_verbatim_on_the_lane_row() -> None:
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(
        rows, topic=TOPIC_RUNTIME_HEALTH, payload=health_event(status="DEGRADED")
    )

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["health_aggregate"] == "DEGRADED"
    assert wire["health_status"] == EnumFactStatus.WARN.value
    assert [d["name"] for d in wire["health_dimensions"]] == [
        "contract_discovery",
        "consumer_groups",
    ]


def test_ac2_a_health_event_naming_no_lane_is_dropped_not_guessed() -> None:
    """A runtime that cannot name its lane must not land on the dev lane's row.

    This is the pre-OMN-18769 event shape. Attributing it to compose-dev by
    proximity would put another cluster's verdict on the lab's row.
    """
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    payload = health_event()
    del payload["lane"]

    assert apply_event(rows, topic=TOPIC_RUNTIME_HEALTH, payload=payload) == ()
    assert rows == {}


def test_ac2_an_unrecognised_aggregate_is_unknown_never_pass() -> None:
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(
        rows, topic=TOPIC_RUNTIME_HEALTH, payload=health_event(status="SPLENDID")
    )

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["health_original_status"] == EnumFactStatus.UNKNOWN.value


# --------------------------------------------------------------------------
# AC3 — a FAIL receipt is as renderable as a PASS one
# --------------------------------------------------------------------------


def test_ac3_a_fail_receipt_carries_its_failing_check_names_onto_the_row() -> None:
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(
        rows,
        topic=TOPIC_LAB_PASS_RECEIPT,
        payload=receipt_event(
            result="FAIL", failing=("ready_effects", "projection_ready")
        ),
    )

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["receipt_result"] == "FAIL"
    assert wire["receipt_status"] == EnumFactStatus.FAIL.value
    assert wire["receipt_failing_checks"] == ["ready_effects", "projection_ready"]


def test_ac3_no_receipt_yet_is_unknown_never_pass() -> None:
    """An absent producer renders as unproduced, never as green."""
    row = ModelLabLaneHealthRow(lane=EnumLabLane.COMPOSE_DEV)
    wire = row.to_exposure_row(now=NOW)

    assert wire["receipt_status"] == EnumFactStatus.UNKNOWN.value
    assert wire["receipt_observed_at"] is None


# --------------------------------------------------------------------------
# AC5 — per-fact freshness: the ticket's own falsifier
# --------------------------------------------------------------------------


def test_ac5_a_fresh_health_fact_does_not_make_a_30_hour_old_census_look_current() -> (
    None
):
    """The ticket's falsifier, verbatim.

    Feed a fresh health fact alongside a 30-hour-old census fact; assert the
    census dimension reports STALE while the health dimension reports its live
    verdict, with both PRE-DECAY verdicts preserved.
    """
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(
        rows,
        topic=TOPIC_LANE_CENSUS,
        payload=census_event(drift=0, at=NOW - timedelta(hours=30)),
    )
    apply_event(
        rows, topic=TOPIC_RUNTIME_HEALTH, payload=health_event(status="HEALTHY")
    )

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)

    assert wire["census_status"] == EnumFactStatus.STALE.value
    assert wire["census_original_status"] == EnumFactStatus.PASS.value
    assert wire["health_status"] == EnumFactStatus.PASS.value
    assert wire["health_original_status"] == EnumFactStatus.PASS.value
    # And each fact carries its OWN timestamp, which is the mechanism.
    assert wire["census_observed_at"] != wire["health_observed_at"]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(minutes=1), EnumFactStatus.PASS),
        (timedelta(hours=7, minutes=59), EnumFactStatus.PASS),
        (timedelta(hours=8), EnumFactStatus.WARN),
        (timedelta(hours=23, minutes=59), EnumFactStatus.WARN),
        (timedelta(hours=24), EnumFactStatus.STALE),
    ],
)
def test_ac5_pass_decays_at_eight_hours_and_twenty_four(
    age: timedelta, expected: EnumFactStatus
) -> None:
    assert decay(EnumFactStatus.PASS, NOW - age, now=NOW) is expected


def test_ac5_a_failure_does_not_decay_into_a_stale_shrug() -> None:
    """Age neither repairs nor deepens a FAIL.

    Decaying a FAIL to STALE would let a drifted lane read as merely old, which
    is the direction that hides an outage.
    """
    assert (
        decay(EnumFactStatus.FAIL, NOW - timedelta(days=9), now=NOW)
        is EnumFactStatus.FAIL
    )


def test_ac5_a_fact_with_no_timestamp_is_unknown_not_fresh() -> None:
    assert decay(EnumFactStatus.PASS, None, now=NOW) is EnumFactStatus.UNKNOWN


def test_ac5_clock_skew_from_a_lab_host_is_age_zero_not_an_error() -> None:
    assert (
        decay(EnumFactStatus.PASS, NOW + timedelta(minutes=3), now=NOW)
        is EnumFactStatus.PASS
    )


# --------------------------------------------------------------------------
# AC6 — the lab, and nothing else
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out_of_scope", ["stability-test", "judge", "lakshman", "prod"]
)
def test_ac6_no_row_can_be_keyed_on_a_lane_outside_the_lab(out_of_scope: str) -> None:
    assert normalize_lane(out_of_scope) is None


def test_ac6_a_census_run_covering_other_lanes_materializes_only_lab_rows() -> None:
    """The census checks four lanes; this projection holds one of them.

    The mechanism is the alias map, not a filter the caller has to remember to
    apply -- a lane with no alias cannot be keyed at all.
    """
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(rows, topic=TOPIC_LANE_CENSUS, payload=census_event(drift=0))

    assert set(rows) == {EnumLabLane.COMPOSE_DEV}


def test_ac6_another_lanes_drift_is_not_counted_onto_the_dev_lanes_row() -> None:
    payload = census_event(drift=1)
    findings = list(payload["findings"])  # type: ignore[arg-type]
    findings.append(
        {
            "lane": "stability-test",
            "kind": "absent_container",
            "container": "omninode-runtime",
            "detail": "declared but not running",
            "severity": "critical",
        }
    )
    payload["findings"] = findings

    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(rows, topic=TOPIC_LANE_CENSUS, payload=payload)

    assert (
        rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)["census_drift_count"]
        == 1
    )


# --------------------------------------------------------------------------
# Fold invariants the ACs rest on
# --------------------------------------------------------------------------


def test_only_the_lanes_an_event_touches_are_republished() -> None:
    """An unchanged lane is not republished with a newer projected_at.

    Republishing every row on every event would stamp a freshness claim nothing
    observed onto lanes the event never mentioned.
    """
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {
        EnumLabLane.ONEX_LAB: ModelLabLaneHealthRow(lane=EnumLabLane.ONEX_LAB),
    }
    touched = apply_event(rows, topic=TOPIC_RUNTIME_HEALTH, payload=health_event())

    assert [row.lane for row in touched] == [EnumLabLane.COMPOSE_DEV]


def test_a_later_fact_replaces_only_its_own_dimension() -> None:
    rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
    apply_event(rows, topic=TOPIC_LANE_CENSUS, payload=census_event(drift=2))
    apply_event(rows, topic=TOPIC_LAB_PASS_RECEIPT, payload=receipt_event())
    apply_event(rows, topic=TOPIC_RUNTIME_HEALTH, payload=health_event())

    wire = rows[EnumLabLane.COMPOSE_DEV].to_exposure_row(now=NOW)
    assert wire["census_drift_count"] == 2
    assert wire["receipt_result"] == "PASS"
    assert wire["health_aggregate"] == "HEALTHY"


def test_an_unparseable_timestamp_is_refused_not_defaulted_to_now() -> None:
    """A malformed event goes to the DLQ; it never lands as a fresh fact."""
    payload = census_event()
    payload["observed_at"] = "the other day"

    with pytest.raises(LaneHealthFoldError):
        apply_event({}, topic=TOPIC_LANE_CENSUS, payload=payload)


def test_an_unsubscribed_topic_is_refused() -> None:
    with pytest.raises(LaneHealthFoldError):
        apply_event({}, topic="onex.evt.somewhere.else.v1", payload={})


# --------------------------------------------------------------------------
# AC4 — the exposure is bus-backed, and its wiring matches the code
# --------------------------------------------------------------------------


def test_ac4_the_exposure_is_declared_bus_backed_and_keyed_on_lane() -> None:
    """A bus_backed flag is only honest if its writer publishes in the same change.

    This asserts the contract half; the writer half is
    ``HandlerProjectionLabLaneHealth._republish``, and the live readback is in
    the PR body.
    """
    from pathlib import Path

    import yaml

    from omnimarket.projection.discovery import load_projection_exposures_from_contract

    path = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_projection_lab_lane_health/contract.yaml"
    )
    contract = yaml.safe_load(path.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, contract["name"], path
    )

    exposure = next(e for e in exposures if e.bus_backed)
    assert exposure.topic == "onex.snapshot.projection.lab.lane-health.v1"
    assert exposure.key_columns == ("lane",)
    assert exposure.status.value == "ok"


def test_the_contract_and_the_fold_agree_on_the_subscribed_topics() -> None:
    """A topic declared in one place and not the other is silently unconsumed."""
    from pathlib import Path

    import yaml

    path = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_projection_lab_lane_health/contract.yaml"
    )
    contract = yaml.safe_load(path.read_text())
    declared = contract["event_bus"]["subscribe_topics"]

    assert sorted(declared) == sorted(
        [TOPIC_LANE_CENSUS, TOPIC_RUNTIME_HEALTH, TOPIC_LAB_PASS_RECEIPT]
    )


def test_a_stored_record_round_trips_back_into_the_same_wire_row() -> None:
    """The republish path reads the row BACK, so the decode must be lossless.

    A health tick republishes the census and receipt dimensions alongside it.
    If the decode dropped a dimension, every health tick would blank the other
    two on the consumer's cached row -- a regression that looks like data loss
    and would be blamed on the producer.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        row_from_record,
    )

    record = {
        "lane": "compose-dev",
        "census_observed_at": NOW,
        "census_drift_count": 2,
        "census_drift_items": '[{"lane": "dev", "kind": "unexpected_container"}]',
        "census_host": LAB_HOST,
        "health_observed_at": NOW,
        "health_aggregate": "DEGRADED",
        "health_dimensions": '[{"name": "consumer_groups", "status": "DEGRADED"}]',
        "receipt_observed_at": NOW,
        "receipt_sha": "b" * 40,
        "receipt_result": "FAIL",
        "receipt_failing_checks": '["ready_effects"]',
    }

    wire = row_from_record(record).to_exposure_row(now=NOW)

    assert wire["lane"] == "compose-dev"
    assert wire["census_drift_count"] == 2
    assert wire["census_status"] == EnumFactStatus.FAIL.value
    assert wire["health_aggregate"] == "DEGRADED"
    assert wire["receipt_failing_checks"] == ["ready_effects"]
