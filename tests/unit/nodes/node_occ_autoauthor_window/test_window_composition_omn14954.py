# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Representative-N composition tests (OMN-14954, rolling-plan lane A7).

The N=10 window criterion is a *representative* window, not a bare count:
10 distinct ``(product_repo, product_pr, head_sha, policy_version)`` source
tuples (reruns collapse), fail-reset on any non-clean representative, AND a
composition floor of >=3 merged-path + >=1 runtime/deploy-gated observations
inside the trailing clean streak.

Red-then-green provenance: on the pre-OMN-14954 streak-only logic,
``test_bare_observations_can_no_longer_certify_flip_ready`` FAILS (the old
window returned ``flip_ready=True`` for 10 clean bare observations with no
tuple identity and no composition evidence) and every record-mode test fails
because the fields do not exist. Captured red run is cited in the PR body.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import (
    EnumOccVerificationPath,
    ModelOccObservationRecord,
    project_qualifying_observations,
    project_qualifying_records,
)
from omnimarket.events.occ_observation_store import (
    parse_occ_observation_record,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)

_REPO = "OmniNode-ai/omnimarket"


def _obs(pr: int, *, clean: bool = True, ts: str) -> ModelOccAutoauthorObservation:
    return ModelOccAutoauthorObservation(
        product_repo=_REPO,
        product_pr_number=pr,
        occ_pr_number=4000 + pr,
        minted_by_node=clean,
        attestation_match=clean,
        occ_preflight_eligible=clean,
        observed_at=ts,
    )


def _rec(
    i: int,
    *,
    path: EnumOccVerificationPath = EnumOccVerificationPath.UNSPECIFIED,
    clean: bool = True,
    pr: int | None = None,
    head_sha: str | None = None,
    run_id: int | None = None,
    attempt: int = 1,
    ts: str | None = None,
) -> ModelOccObservationRecord:
    """One record; distinct source tuple per ``i`` unless pr/head_sha pinned."""
    pr = pr if pr is not None else 100 + i
    ts = ts or f"2026-07-22T00:{i:02d}:00Z"
    return ModelOccObservationRecord(
        product_repo=_REPO,
        product_pr_number=pr,
        head_sha=head_sha or f"{i:040d}",
        policy_version="v1",
        workflow_run_id=run_id if run_id is not None else 5000 + i,
        run_attempt=attempt,
        recorded_at=ts,
        verification_path=path,
        observation=_obs(pr, clean=clean, ts=ts),
    )


def _representative_ten(
    *, merged: int = 3, gated: int = 1
) -> list[ModelOccObservationRecord]:
    """Ten distinct clean tuples with ``merged`` merged-path + ``gated`` gated."""
    paths = (
        [EnumOccVerificationPath.MERGED_PATH] * merged
        + [EnumOccVerificationPath.RUNTIME_DEPLOY_GATED] * gated
        + [EnumOccVerificationPath.UNSPECIFIED] * (10 - merged - gated)
    )
    return [_rec(i, path=paths[i]) for i in range(10)]


# ---------------------------------------------------------------------------
# The retired streak-only shortcut (RED on pre-OMN-14954 logic).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_observations_can_no_longer_certify_flip_ready() -> None:
    """10 clean bare observations report the streak but withhold flip_ready.

    Composition (>=3 merged-path, >=1 runtime/deploy-gated) is unverifiable
    without tuple-keyed records, so the absent input FAILS the check — it does
    not silently pass it (feedback_optional_input_means_the_check_does_not_exist).
    """
    obs = tuple(_obs(i, ts=f"2026-07-22T00:{i:02d}:00Z") for i in range(1, 11))
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(observations=obs, required_streak=10)
    )
    assert result.consecutive_clean == 10  # streak still reported
    assert result.composition_met is False
    assert result.flip_ready is False  # was True under streak-only logic
    assert "composition" in result.summary


# ---------------------------------------------------------------------------
# Distinct-tuple dedup inside the window (record mode).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reruns_of_same_tuple_collapse_to_one() -> None:
    """12 raw records over 9 distinct tuples -> streak 9, never 12."""
    records = _representative_ten()[:9]  # 9 distinct clean tuples
    # Tuple collisions: 3 extra attempts at record 0's exact source tuple.
    for attempt in (2, 3, 4):
        records.append(
            _rec(
                0,
                path=EnumOccVerificationPath.MERGED_PATH,
                run_id=5000,
                attempt=attempt,
            )
        )
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.total_observations == 12
    assert result.distinct_tuples == 9
    assert result.consecutive_clean == 9
    assert result.flip_ready is False


@pytest.mark.unit
def test_later_clean_rerun_shadows_earlier_fail_soft() -> None:
    """A tuple whose first attempt fail-softed but whose rerun is clean counts
    clean — the representative is the most recent attempt, and the stale
    fail-soft attempt does not fabricate a reset."""
    records = _representative_ten()
    # Earlier fail-soft attempt at record 9's exact source tuple.
    records.append(
        _rec(9, clean=False, run_id=5009, attempt=1, ts="2026-07-22T00:09:00Z")
    )
    records[9] = _rec(
        9,
        path=records[9].verification_path,
        run_id=5009,
        attempt=2,
        ts="2026-07-22T00:09:00Z",
    )
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.distinct_tuples == 10
    assert result.consecutive_clean == 10
    assert result.flip_ready is True


@pytest.mark.unit
def test_non_clean_representative_resets_streak() -> None:
    records = [_rec(i) for i in range(4)]
    records.append(_rec(4, clean=False))
    records.extend(_rec(i) for i in range(5, 8))
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.distinct_tuples == 8
    assert result.consecutive_clean == 3
    assert result.flip_ready is False
    assert result.streak_broken_by == f"{_REPO}#104"


# ---------------------------------------------------------------------------
# Composition thresholds (>=3 merged-path, >=1 runtime/deploy-gated).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_merged_path_without_runtime_gated_is_not_flip_ready() -> None:
    """The core A7 criterion: 10 clean distinct tuples that are ALL merged-path
    hit the streak but are not representative — no runtime/deploy-gated proof."""
    records = _representative_ten(merged=10, gated=0)
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.consecutive_clean == 10
    assert result.merged_path_clean == 10
    assert result.runtime_gated_clean == 0
    assert result.composition_met is False
    assert result.flip_ready is False


@pytest.mark.unit
def test_composition_met_with_3_merged_1_runtime_gated() -> None:
    records = _representative_ten(merged=3, gated=1)
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.merged_path_clean == 3
    assert result.runtime_gated_clean == 1
    assert result.composition_met is True
    assert result.flip_ready is True


@pytest.mark.unit
def test_unspecified_counts_toward_no_threshold() -> None:
    records = _representative_ten(merged=0, gated=0)  # all unspecified
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.consecutive_clean == 10
    assert result.composition_met is False
    assert result.flip_ready is False


@pytest.mark.unit
def test_composition_counted_over_trailing_streak_only() -> None:
    """Merged-path tuples stranded BEFORE a reset do not satisfy composition."""
    records = [
        _rec(i, path=EnumOccVerificationPath.MERGED_PATH, pr=50 + i) for i in range(3)
    ]
    records.append(_rec(3, clean=False, pr=53))  # reset
    trailing = _representative_ten(merged=2, gated=1)
    # shift trailing timestamps after the reset
    records.extend(
        _rec(
            i + 4,
            path=t.verification_path,
            pr=t.product_pr_number,
            ts=f"2026-07-22T01:{i:02d}:00Z",
        )
        for i, t in enumerate(trailing)
    )
    result = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=tuple(records), required_streak=10)
    )
    assert result.consecutive_clean == 10
    assert result.merged_path_clean == 2  # pre-reset merged tuples do not count
    assert result.composition_met is False
    assert result.flip_ready is False


# ---------------------------------------------------------------------------
# Seam / contract guarantees.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_records_and_observations_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ModelOccAutoauthorWindowRequest(
            records=(_rec(0),),
            observations=(_obs(1, ts="2026-07-22T00:00:00Z"),),
        )


@pytest.mark.unit
def test_old_shape_record_parses_with_unspecified_path() -> None:
    """Rows persisted before OMN-14954 (no verification_path) still parse."""
    old_shape = _rec(0).model_dump()
    del old_shape["verification_path"]
    record = ModelOccObservationRecord.model_validate(old_shape)
    assert record.verification_path is EnumOccVerificationPath.UNSPECIFIED


@pytest.mark.unit
def test_store_roundtrip_preserves_verification_path() -> None:
    record = _rec(0, path=EnumOccVerificationPath.RUNTIME_DEPLOY_GATED)
    assert parse_occ_observation_record(render_occ_observation_record(record)) == record


@pytest.mark.unit
def test_project_qualifying_records_parity_with_observations() -> None:
    """The record projection is the same dedup; the observation projection is
    exactly its stripped form (no behavior change for existing consumers)."""
    records = _representative_ten()
    records.append(_rec(0, run_id=5000, attempt=2))  # rerun collision
    reps = project_qualifying_records(records)
    assert len(reps) == 10
    assert project_qualifying_observations(records) == tuple(
        r.observation for r in reps
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_record_mode_matches_pure_function() -> None:
    request = ModelOccAutoauthorWindowRequest(
        records=tuple(_representative_ten()), required_streak=10
    )
    via_handler = await HandlerOccAutoauthorWindow().handle(request)
    assert via_handler == aggregate_autoauthor_window(request)
    assert via_handler.flip_ready is True
