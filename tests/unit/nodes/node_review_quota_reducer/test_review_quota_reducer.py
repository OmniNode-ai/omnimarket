# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review quota reducer and its preregistration (OMN-21187).

AC1: the preregistration record fixes the quotas, eligible routes, minimum
counts, pilot window and cutting rule, and its sha256 sidecar equals the hash of
the file. AC2: the reducer admits a review only while its route's weekly quota
has room and refuses it otherwise.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_review_quota import (
    EnumReviewQuotaCommand,
    EnumReviewQuotaEntryStatus,
    EnumReviewQuotaReason,
)
from omnimarket.nodes.node_review_quota_reducer import HandlerReviewQuotaReducer
from omnimarket.nodes.node_review_quota_reducer.handlers.handler_review_quota_reducer import (
    PreregistrationHashMismatchError,
    load_preregistration,
)
from omnimarket.nodes.node_review_quota_reducer.models import (
    ModelReviewQuotaEntry,
    ModelReviewQuotaRequest,
    ModelReviewQuotaState,
    RouteNotEligibleError,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_review_quota_reducer"
)
PREREG = NODE_DIR / "preregistration.v1.yaml"
SIDECAR = NODE_DIR / "preregistration.v1.yaml.sha256"

START = datetime(2026, 11, 1, tzinfo=UTC)
ROUTE = "claude_sonnet_review_voter"


def _prereg_hash() -> str:
    return hashlib.sha256(PREREG.read_bytes()).hexdigest()


def _request(
    request_id: str,
    *,
    route_id: str = ROUTE,
    command: EnumReviewQuotaCommand = EnumReviewQuotaCommand.RESERVE,
    as_of: datetime = START + timedelta(days=1),
    started: datetime | None = START,
    state: ModelReviewQuotaState | None = None,
    prereg_sha: str | None = None,
) -> ModelReviewQuotaRequest:
    return ModelReviewQuotaRequest(
        command=command,
        request_id=request_id,
        route_id=route_id,
        as_of=as_of,
        collection_started_at=started,
        preregistration_sha256=prereg_sha or _prereg_hash(),
        state=state or ModelReviewQuotaState(),
    )


def _state_with(
    count: int, *, route_id: str = ROUTE, window: int = 0
) -> ModelReviewQuotaState:
    return ModelReviewQuotaState(
        entries=tuple(
            ModelReviewQuotaEntry(
                request_id=f"seed-{route_id}-{window}-{i}",
                route_id=route_id,
                window_index=window,
                status=EnumReviewQuotaEntryStatus.RESERVED,
            )
            for i in range(count)
        )
    )


# --- AC1: the preregistration record -------------------------------------


def test_sidecar_hash_equals_sha256_of_the_preregistration_file() -> None:
    """The falsifier of AC1: sha256sum of the file equals the hash the record states."""
    stated, name = SIDECAR.read_text(encoding="utf-8").split()
    assert name == PREREG.name
    assert stated == _prereg_hash()


def test_preregistration_fixes_every_required_section() -> None:
    record = yaml.safe_load(PREREG.read_text(encoding="utf-8"))
    assert record["status"] == "sealed-before-collection"
    assert record["collection"]["pilot_days"] == 14
    assert record["collection"]["window_days"] == 30
    assert record["collection"]["pilot_excluded_from_evaluation"] is True
    routes = {r["route_id"]: r for r in record["eligible_routes"]}
    assert routes
    assert all(r["weekly_quota"] >= 1 for r in routes.values())
    assert {
        "labeled_findings_real",
        "labeled_findings_false",
        "negative_evidence_sample",
    } <= set(record["minimum_counts"])
    assert record["gate_behaviour"]["on_any_minimum_unmet"]
    buckets = record["cutting_rule"]["buckets"]
    covered = sorted(
        b for lo_hi in buckets.values() for b in range(lo_hi[0], lo_hi[1] + 1)
    )
    assert covered == list(range(100)), "train/evaluation/final must partition 0..99"
    assert record["final_acceptance"]["failed_final_candidate_permanently_rejected"]


def test_changed_preregistration_bytes_do_not_load() -> None:
    tampered = PREREG.read_bytes() + b"# edited after sealing\n"
    with pytest.raises(PreregistrationHashMismatchError):
        load_preregistration(tampered, SIDECAR.read_text(encoding="utf-8"))


def test_unchanged_preregistration_loads() -> None:
    prereg = load_preregistration()
    assert prereg.sha256 == _prereg_hash()
    assert prereg.quota_for(ROUTE) == 120


def test_quota_for_an_unlisted_route_raises() -> None:
    prereg = load_preregistration()
    assert prereg.is_eligible("some_unlisted_route") is False
    assert prereg.is_eligible(ROUTE) is True
    with pytest.raises(RouteNotEligibleError, match="some_unlisted_route"):
        prereg.quota_for("some_unlisted_route")


# --- AC2: admission against the route quota -------------------------------


def test_under_quota_fixture_is_admitted() -> None:
    result = HandlerReviewQuotaReducer().handle(
        _request("r-new", state=_state_with(119))
    )
    assert result.applied is True
    assert result.reason is EnumReviewQuotaReason.ADMITTED
    assert result.used_in_window == 120
    assert result.quota == 120
    assert len(result.state.entries) == 120


def test_at_quota_fixture_is_refused_and_state_is_unchanged() -> None:
    state = _state_with(120)
    result = HandlerReviewQuotaReducer().handle(_request("r-new", state=state))
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.QUOTA_EXHAUSTED
    assert result.used_in_window == 120
    assert result.state == state


def test_route_not_on_the_preregistered_list_is_refused() -> None:
    state = _state_with(1)
    result = HandlerReviewQuotaReducer().handle(
        _request("r-new", route_id="some_unlisted_route", state=state)
    )
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.ROUTE_NOT_ELIGIBLE
    assert result.quota is None
    assert result.state == state


def test_quota_is_per_route() -> None:
    state = _state_with(120, route_id=ROUTE)
    result = HandlerReviewQuotaReducer().handle(
        _request("r-new", route_id="claude_opus_review", state=state)
    )
    assert result.applied is True


def test_quota_is_per_week_and_next_week_has_room() -> None:
    state = _state_with(120, window=0)
    result = HandlerReviewQuotaReducer().handle(
        _request("r-new", as_of=START + timedelta(days=7), state=state)
    )
    assert result.applied is True
    assert result.window_index == 1


def test_same_request_id_is_idempotent_even_at_quota() -> None:
    handler = HandlerReviewQuotaReducer()
    first = handler.handle(_request("r-1", state=_state_with(119)))
    assert first.applied is True
    replay = handler.handle(_request("r-1", state=first.state))
    assert replay.applied is True
    assert replay.reason is EnumReviewQuotaReason.REPLAYED
    assert replay.state == first.state
    assert replay.used_in_window == 120


def test_request_id_reused_on_another_route_is_refused() -> None:
    handler = HandlerReviewQuotaReducer()
    first = handler.handle(_request("r-1"))
    clash = handler.handle(
        _request("r-1", route_id="claude_opus_review", state=first.state)
    )
    assert clash.applied is False
    assert clash.reason is EnumReviewQuotaReason.REQUEST_ID_CONFLICT


def test_no_collection_start_refuses() -> None:
    result = HandlerReviewQuotaReducer().handle(_request("r-1", started=None))
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.COLLECTION_NOT_STARTED


def test_before_collection_start_refuses() -> None:
    result = HandlerReviewQuotaReducer().handle(
        _request("r-1", as_of=START - timedelta(seconds=1))
    )
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.COLLECTION_NOT_STARTED


def test_after_the_collection_window_refuses() -> None:
    result = HandlerReviewQuotaReducer().handle(
        _request("r-1", as_of=START + timedelta(days=30))
    )
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.COLLECTION_CLOSED


def test_phase_is_pilot_for_the_first_14_days_then_main() -> None:
    handler = HandlerReviewQuotaReducer()
    pilot = handler.handle(_request("r-1", as_of=START + timedelta(days=13, hours=23)))
    main = handler.handle(_request("r-2", as_of=START + timedelta(days=14)))
    assert pilot.phase == "pilot"
    assert main.phase == "main"


def test_request_naming_another_preregistration_hash_is_refused() -> None:
    result = HandlerReviewQuotaReducer().handle(_request("r-1", prereg_sha="0" * 64))
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.PREREGISTRATION_MISMATCH


def test_consume_marks_the_reservation_and_keeps_it_counted() -> None:
    handler = HandlerReviewQuotaReducer()
    reserved = handler.handle(_request("r-1"))
    consumed = handler.handle(
        _request("r-1", command=EnumReviewQuotaCommand.CONSUME, state=reserved.state)
    )
    assert consumed.applied is True
    assert consumed.reason is EnumReviewQuotaReason.CONSUMED
    assert consumed.used_in_window == 1
    assert consumed.state.entries[0].status is EnumReviewQuotaEntryStatus.CONSUMED


def test_release_returns_a_reservation_that_never_reached_the_model() -> None:
    handler = HandlerReviewQuotaReducer()
    reserved = handler.handle(_request("r-1", state=_state_with(119)))
    released = handler.handle(
        _request("r-1", command=EnumReviewQuotaCommand.RELEASE, state=reserved.state)
    )
    assert released.applied is True
    assert released.reason is EnumReviewQuotaReason.RELEASED
    assert released.used_in_window == 119
    again = handler.handle(_request("r-2", state=released.state))
    assert again.applied is True


def test_consumed_entry_cannot_be_released() -> None:
    handler = HandlerReviewQuotaReducer()
    reserved = handler.handle(_request("r-1"))
    consumed = handler.handle(
        _request("r-1", command=EnumReviewQuotaCommand.CONSUME, state=reserved.state)
    )
    refused = handler.handle(
        _request("r-1", command=EnumReviewQuotaCommand.RELEASE, state=consumed.state)
    )
    assert refused.applied is False
    assert refused.reason is EnumReviewQuotaReason.ALREADY_CONSUMED


def test_consume_of_an_unknown_request_is_refused() -> None:
    result = HandlerReviewQuotaReducer().handle(
        _request("never-reserved", command=EnumReviewQuotaCommand.CONSUME)
    )
    assert result.applied is False
    assert result.reason is EnumReviewQuotaReason.UNKNOWN_REQUEST
