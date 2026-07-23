# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain acceptance test (OMN-14888): the full durable-storage chain from
raw OCC-committed files to the N=10 flip-ready counter.

    write EFFECT (render+path convention)
        -> durable files on disk (standing in for a committed onex_change_control tree)
        -> read EFFECT (node_occ_observation_source_effect, "OCC as source")
        -> project_qualifying_observations (OMN-14851, unmodified)
        -> aggregate_autoauthor_window (OMN-14393, unmodified)

Proves the exact three acceptance scenarios the OMN-14888 ticket requires:

  (a) 10 reruns of the identical source tuple count as 1, not 10.
  (b) 10 distinct source tuples count as 10 (flip_ready at required_streak=10).
  (c) a fail-soft (non-clean) observation is durably recorded (present in the
      raw log, retrievable) but does NOT qualify toward the clean streak.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import (
    EnumOccVerificationPath,
    ModelOccObservationRecord,
)
from omnimarket.events.occ_observation_store import (
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_observation_source_effect.handlers.handler_occ_observation_source_effect import (
    HandlerOccObservationSourceEffect,
)
from omnimarket.nodes.node_occ_observation_source_effect.models.model_occ_observation_source_effect_request import (
    ModelOccObservationSourceEffectRequest,
)


def _clean_record(
    *,
    head_sha: str,
    workflow_run_id: int,
    run_attempt: int = 1,
    observed_at: str,
    path: EnumOccVerificationPath = EnumOccVerificationPath.UNSPECIFIED,
) -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1841,
        head_sha=head_sha,
        policy_version="v1",
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        recorded_at=observed_at,
        verification_path=path,
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1841,
            occ_pr_number=4500 + workflow_run_id,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at=observed_at,
            reason="",
        ),
    )


def _fail_soft_record(
    *, head_sha: str, workflow_run_id: int, observed_at: str
) -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1841,
        head_sha=head_sha,
        policy_version="v1",
        workflow_run_id=workflow_run_id,
        run_attempt=1,
        recorded_at=observed_at,
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1841,
            occ_pr_number=None,
            minted_by_node=False,
            attestation_match=False,
            occ_preflight_eligible=False,
            observed_at=observed_at,
            reason="observation error (report-only, non-blocking): GitHubApiError",
        ),
    )


def _durably_write(checkout_dir: Path, record: ModelOccObservationRecord) -> None:
    """Stands in for node_occ_observation_effect's git-append-write: the SAME
    deterministic path convention + deterministic render, materialized to disk."""
    relpath = occ_observation_record_relpath(record)
    path = checkout_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_occ_observation_record(record), encoding="utf-8")


async def _load(checkout_dir: Path) -> tuple:
    result = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(checkout_dir))
    )
    return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ten_reruns_of_the_identical_tuple_count_as_one(tmp_path: Path) -> None:
    for attempt in range(1, 11):
        _durably_write(
            tmp_path,
            _clean_record(
                head_sha="c" * 40,
                workflow_run_id=999,
                run_attempt=attempt,
                observed_at="2026-07-21T00:00:00Z",
                path=EnumOccVerificationPath.MERGED_PATH,
            ),
        )

    result = await _load(tmp_path)
    assert result.raw_record_count == 10
    assert result.distinct_source_tuples == 1

    # Record mode (OMN-14954): the window dedupes the raw log itself. A single
    # distinct tuple cannot meet the default composition floor, so thresholds
    # are set explicitly to keep this test about DEDUP, not composition.
    window = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(
            records=result.records,
            required_streak=1,
            min_merged_path=1,
            min_runtime_gated=0,
        )
    )
    assert window.consecutive_clean == 1
    assert window.distinct_tuples == 1
    assert window.total_observations == 10  # raw rows, deduped to 1 tuple
    assert window.flip_ready is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ten_distinct_clean_tuples_reach_flip_ready_at_n10(
    tmp_path: Path,
) -> None:
    paths = (
        [EnumOccVerificationPath.MERGED_PATH] * 3
        + [EnumOccVerificationPath.RUNTIME_DEPLOY_GATED]
        + [EnumOccVerificationPath.UNSPECIFIED] * 6
    )
    for i in range(10):
        _durably_write(
            tmp_path,
            _clean_record(
                head_sha=f"{i:040d}",
                workflow_run_id=i,
                observed_at=f"2026-07-21T00:0{i}:00Z",
                path=paths[i],
            ),
        )

    result = await _load(tmp_path)
    assert result.raw_record_count == 10
    assert result.distinct_source_tuples == 10

    window = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=result.records, required_streak=10)
    )
    assert window.consecutive_clean == 10
    assert window.distinct_tuples == 10
    assert window.composition_met is True
    assert window.flip_ready is True
    assert window.streak_broken_by == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fail_soft_observation_is_durable_but_does_not_qualify(
    tmp_path: Path,
) -> None:
    # 9 clean, chronologically-earliest, then 1 fail-soft LAST — the fail-soft
    # attempt is durably recorded (present + counted in raw_record_count / the
    # projection) but resets the trailing clean streak, so N=10 is NOT reached.
    for i in range(9):
        _durably_write(
            tmp_path,
            _clean_record(
                head_sha=f"{i:040d}",
                workflow_run_id=i,
                observed_at=f"2026-07-21T00:0{i}:00Z",
            ),
        )
    _durably_write(
        tmp_path,
        _fail_soft_record(
            head_sha="f" * 40, workflow_run_id=999, observed_at="2026-07-21T00:09:00Z"
        ),
    )

    result = await _load(tmp_path)
    # The fail-soft attempt IS durably recorded and retrievable — never
    # silently dropped from the append-only trail.
    assert result.raw_record_count == 10
    assert result.distinct_source_tuples == 10
    assert any(not o.is_clean for o in result.observations)

    window = aggregate_autoauthor_window(
        ModelOccAutoauthorWindowRequest(records=result.records, required_streak=10)
    )
    # It does NOT qualify toward the clean streak: the trailing (most recent)
    # observation is the fail-soft one, so the streak resets to zero.
    assert window.consecutive_clean == 0
    assert window.flip_ready is False
    assert window.streak_broken_by == "OmniNode-ai/omnimarket#1841"
