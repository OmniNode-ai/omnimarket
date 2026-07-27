# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests: node_occ_observation_source_effect reads a real filesystem tree
and dedupes via the EXISTING, unmodified project_qualifying_observations."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATIONS_ROOT,
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_observation_source_effect.handlers.handler_occ_observation_source_effect import (
    HandlerOccObservationSourceEffect,
)
from omnimarket.nodes.node_occ_observation_source_effect.models.model_occ_observation_source_effect_request import (
    ModelOccObservationSourceEffectRequest,
)


def _record(
    *,
    head_sha: str = "a" * 40,
    workflow_run_id: int = 1,
    run_attempt: int = 1,
    minted_by_node: bool = True,
    attestation_match: bool = True,
    occ_preflight_eligible: bool = True,
    observed_at: str = "2026-07-21T00:00:00Z",
) -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1841,
        head_sha=head_sha,
        policy_version="v1",
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        recorded_at=observed_at,
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1841,
            occ_pr_number=4500,
            minted_by_node=minted_by_node,
            attestation_match=attestation_match,
            occ_preflight_eligible=occ_preflight_eligible,
            observed_at=observed_at,
            reason="",
        ),
    )


def _write(checkout_dir: Path, record: ModelOccObservationRecord) -> None:
    relpath = occ_observation_record_relpath(record)
    path = checkout_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_occ_observation_record(record), encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_store_root_errors(tmp_path: Path) -> None:
    """OMN-14906: an ABSENT store must fail closed, not read as an empty one.

    This test previously asserted the opposite (a checkout with no
    ``drift/occ_observations/`` tree returned ``raw_record_count=0``). That
    codified the "optional input that silently skips" defect: the read reported
    a clean zero for a store that has never existed — which is the live state of
    ``onex_change_control@main``.
    """
    handler = HandlerOccObservationSourceEffect()
    with pytest.raises(FileNotFoundError):
        await handler.handle(
            ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_present_but_empty_store_yields_zero_observations(
    tmp_path: Path,
) -> None:
    """A PRESENT but empty trail is a distinct, valid zero result."""
    (tmp_path / OCC_OBSERVATIONS_ROOT).mkdir(parents=True)

    handler = HandlerOccObservationSourceEffect()
    result = await handler.handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )
    assert result.observations == ()
    assert result.raw_record_count == 0
    assert result.distinct_source_tuples == 0
    assert result.malformed_paths == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ten_reruns_of_the_same_tuple_collapse_to_one(tmp_path: Path) -> None:
    for attempt in range(1, 11):
        _write(tmp_path, _record(workflow_run_id=100, run_attempt=attempt))

    handler = HandlerOccObservationSourceEffect()
    result = await handler.handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 10
    assert result.distinct_source_tuples == 1
    assert len(result.observations) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ten_distinct_tuples_count_as_ten(tmp_path: Path) -> None:
    for i in range(10):
        _write(tmp_path, _record(head_sha=f"{i:040d}", workflow_run_id=i))

    handler = HandlerOccObservationSourceEffect()
    result = await handler.handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 10
    assert result.distinct_source_tuples == 10
    assert len(result.observations) == 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_soft_observation_is_recorded_but_not_clean(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _record(
            head_sha="b" * 40,
            minted_by_node=False,
            attestation_match=False,
            occ_preflight_eligible=False,
        ),
    )

    handler = HandlerOccObservationSourceEffect()
    result = await handler.handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 1
    assert result.distinct_source_tuples == 1
    assert result.observations[0].is_clean is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_record_is_surfaced_not_silently_dropped(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "drift" / "occ_observations" / "junk.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not: a valid record\n", encoding="utf-8")

    handler = HandlerOccObservationSourceEffect()
    result = await handler.handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 0
    assert result.malformed_paths == ("drift/occ_observations/junk.yaml",)
