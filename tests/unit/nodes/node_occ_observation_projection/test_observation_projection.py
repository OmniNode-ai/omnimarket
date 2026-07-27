# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OCC observation dedup projection (OMN-14851).

Acceptance scenarios from the ticket DoD:

  (a) two attempts at the identical head_sha collapse to one qualifying
      observation;
  (b) a failed/fail-soft attempt does not shadow a later clean attempt for the
      same head_sha;
  (c) distinct head_shas for the same PR count as distinct source tuples;
  (d) output is a stable order feeding ModelOccAutoauthorWindowRequest
      unchanged.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import (
    ModelOccObservationRecord,
    occ_observation_raw_key,
    occ_observation_source_tuple,
    project_qualifying_observations,
)
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_observation_projection.handlers.handler_occ_observation_projection import (
    HandlerOccObservationProjection,
    compute_observation_projection,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_request import (
    ModelOccObservationProjectionRequest,
)

_REPO = "OmniNode-ai/omnimarket"


def _record(
    *,
    pr_number: int = 1,
    head_sha: str = "aaa1111",
    policy_version: str = "v2",
    workflow_run_id: int = 100,
    run_attempt: int = 1,
    recorded_at: str = "2026-07-20T00:00:00Z",
    observed_at: str | None = None,
    minted: bool = True,
    match: bool = True,
    eligible: bool = True,
) -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo=_REPO,
        product_pr_number=pr_number,
        head_sha=head_sha,
        policy_version=policy_version,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        recorded_at=recorded_at,
        observation=ModelOccAutoauthorObservation(
            product_repo=_REPO,
            product_pr_number=pr_number,
            occ_pr_number=2000 + pr_number,
            minted_by_node=minted,
            attestation_match=match,
            occ_preflight_eligible=eligible,
            observed_at=observed_at or recorded_at,
        ),
    )


@pytest.mark.unit
def test_raw_key_and_source_tuple_extraction() -> None:
    record = _record(
        pr_number=7, head_sha="deadbeef", workflow_run_id=42, run_attempt=2
    )
    assert occ_observation_raw_key(record) == (
        _REPO,
        7,
        "deadbeef",
        "v2",
        42,
        2,
    )
    assert occ_observation_source_tuple(record) == (_REPO, 7, "deadbeef", "v2")


@pytest.mark.unit
def test_two_attempts_at_identical_head_sha_collapse_to_one_observation() -> None:
    """(a) reruns of the same source tuple never double-count toward N=10."""
    first_attempt = _record(
        workflow_run_id=100, run_attempt=1, recorded_at="2026-07-20T00:00:00Z"
    )
    second_attempt = _record(
        workflow_run_id=100, run_attempt=2, recorded_at="2026-07-20T00:05:00Z"
    )
    projected = project_qualifying_observations((first_attempt, second_attempt))
    assert len(projected) == 1


@pytest.mark.unit
def test_representative_is_the_most_recent_attempt() -> None:
    stale_failed = _record(
        workflow_run_id=100,
        run_attempt=1,
        recorded_at="2026-07-20T00:00:00Z",
        minted=False,  # not clean
    )
    fresh_clean = _record(
        workflow_run_id=100,
        run_attempt=2,
        recorded_at="2026-07-20T00:05:00Z",
        minted=True,
        match=True,
        eligible=True,
    )
    projected = project_qualifying_observations((stale_failed, fresh_clean))
    assert len(projected) == 1
    assert projected[0].is_clean is True


@pytest.mark.unit
def test_failed_attempt_does_not_shadow_a_later_clean_attempt() -> None:
    """(b) an earlier fail-soft attempt at the same head_sha must not win."""
    fail_soft_first = _record(
        workflow_run_id=200,
        run_attempt=1,
        recorded_at="2026-07-20T01:00:00Z",
        minted=False,
        match=False,
        eligible=False,
    )
    clean_retry = _record(
        workflow_run_id=201,
        run_attempt=1,
        recorded_at="2026-07-20T01:10:00Z",
    )
    # Deliberately reversed input order — dedup must not depend on ingestion order.
    projected = project_qualifying_observations((clean_retry, fail_soft_first))
    assert len(projected) == 1
    assert projected[0].is_clean is True


@pytest.mark.unit
def test_distinct_head_shas_for_the_same_pr_are_distinct_source_tuples() -> None:
    """(c) a force-push / new head_sha on the same PR is a NEW source tuple."""
    first_head = _record(
        pr_number=9, head_sha="sha-one", recorded_at="2026-07-20T02:00:00Z"
    )
    second_head = _record(
        pr_number=9, head_sha="sha-two", recorded_at="2026-07-20T02:10:00Z"
    )
    projected = project_qualifying_observations((first_head, second_head))
    assert len(projected) == 2
    assert {o.product_pr_number for o in projected} == {9}


@pytest.mark.unit
def test_defensive_dedup_on_identical_raw_key() -> None:
    """A double-ingested identical attempt (same full 6-tuple) collapses to one row."""
    duplicate_ingestion = _record()
    projected = project_qualifying_observations(
        (duplicate_ingestion, duplicate_ingestion)
    )
    assert len(projected) == 1


@pytest.mark.unit
def test_projection_output_is_deterministically_ordered() -> None:
    a = _record(pr_number=1, head_sha="s1", recorded_at="2026-07-20T03:00:00Z")
    b = _record(pr_number=2, head_sha="s2", recorded_at="2026-07-20T01:00:00Z")
    c = _record(pr_number=3, head_sha="s3", recorded_at="2026-07-20T02:00:00Z")
    p1 = project_qualifying_observations((a, b, c))
    p2 = project_qualifying_observations((c, a, b))
    assert p1 == p2
    assert [o.product_pr_number for o in p1] == [2, 3, 1]  # sorted by observed_at


@pytest.mark.unit
def test_projection_output_feeds_the_window_request_unchanged() -> None:
    """(d) the projection's output is valid, unmodified input to the N=10 window."""
    clean_records = tuple(
        _record(
            pr_number=i,
            head_sha=f"sha-{i}",
            recorded_at=f"2026-07-20T04:{i:02d}:00Z",
        )
        for i in range(1, 11)
    )
    projected = project_qualifying_observations(clean_records)
    window_result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=projected, required_streak=10)
    )
    assert window_result.consecutive_clean == 10
    # OMN-14954: bare observations remain VALID window input and the streak is
    # unchanged, but flip_ready now requires tuple-keyed records (composition
    # is unverifiable here — fail-closed).
    assert window_result.flip_ready is False


@pytest.mark.unit
def test_compute_observation_projection_reports_counts() -> None:
    duplicate_attempt_a = _record(workflow_run_id=1, run_attempt=1)
    duplicate_attempt_b = _record(workflow_run_id=1, run_attempt=2)
    other_tuple = _record(pr_number=2, head_sha="sha-other")
    result = compute_observation_projection(
        ModelOccObservationProjectionRequest(
            records=(duplicate_attempt_a, duplicate_attempt_b, other_tuple)
        )
    )
    assert result.total_raw_records == 3
    assert result.distinct_source_tuples == 2
    assert len(result.observations) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_wraps_the_pure_projection() -> None:
    records = (_record(), _record(workflow_run_id=101, run_attempt=1))
    result = await HandlerOccObservationProjection().handle(
        ModelOccObservationProjectionRequest(records=records)
    )
    assert result.distinct_source_tuples == 1
    assert (
        compute_observation_projection(
            ModelOccObservationProjectionRequest(records=records)
        )
        == result
    )
