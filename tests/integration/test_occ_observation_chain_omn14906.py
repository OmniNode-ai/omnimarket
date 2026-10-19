# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration tests for the OCC observation chain (OMN-14906).

The attestation observer and its HTTP-transport tests are gone with the node
(OMN-17427); the two links that remain are covered here:

* the read side must **ERROR** on an ABSENT observation store rather than
  reporting ``raw_record_count=0`` (an absent store is indistinguishable from an
  empty one — the ``optional input that silently skips`` failure mode); and
* the source → projection → window composition must emit a **concrete streak**.
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
    OCC_OBSERVATIONS_ROOT,
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
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

_PREFLIGHT_CHECK = "occ-preflight / eligibility"
_HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"
_REPO = "OmniNode-ai/omnimarket"


# --------------------------------------------------------------------------- #
# Absent observation store must ERROR, not read as zero                       #
# --------------------------------------------------------------------------- #


def _observation(
    pr: int, *, clean: bool, observed_at: str
) -> ModelOccAutoauthorObservation:
    return ModelOccAutoauthorObservation(
        product_repo=_REPO,
        product_pr_number=pr,
        occ_pr_number=4000 + pr,
        minted_by_node=clean,
        attestation_match=clean,
        occ_preflight_eligible=clean,
        observed_at=observed_at,
        reason="" if clean else "synthetic non-clean observation",
    )


def _record(
    pr: int,
    *,
    clean: bool,
    minute: int,
    path: EnumOccVerificationPath = EnumOccVerificationPath.UNSPECIFIED,
) -> ModelOccObservationRecord:
    stamp = f"2026-07-21T10:{minute:02d}:00+00:00"
    return ModelOccObservationRecord(
        product_repo=_REPO,
        product_pr_number=pr,
        head_sha=f"{pr:040d}",
        policy_version="v1",
        workflow_run_id=900000 + pr,
        run_attempt=1,
        recorded_at=stamp,
        verification_path=path,
        observation=_observation(pr, clean=clean, observed_at=stamp),
    )


def _write_store(root: Path, records: list[ModelOccObservationRecord]) -> None:
    for record in records:
        path = root / occ_observation_record_relpath(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_occ_observation_record(record))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_observation_store_errors_instead_of_reading_zero(
    tmp_path: Path,
) -> None:
    """An ABSENT ``drift/occ_observations/`` must fail closed.

    Verified live 2026-07-21: that tree does not exist on
    ``onex_change_control@main``, so before this fix every read of the durable
    store reported a clean ``raw_record_count=0`` — indistinguishable from a
    genuinely empty store or a mis-pointed checkout.
    """
    handler = HandlerOccObservationSourceEffect()
    request = ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))

    with pytest.raises(FileNotFoundError) as excinfo:
        await handler.handle(request)

    assert OCC_OBSERVATIONS_ROOT in str(excinfo.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_present_but_empty_observation_store_reads_zero(tmp_path: Path) -> None:
    """A genuinely EMPTY (but present) store is a distinct, valid, zero result."""
    (tmp_path / OCC_OBSERVATIONS_ROOT).mkdir(parents=True)

    result = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 0
    assert result.observations == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_checkout_dir_errors(tmp_path: Path) -> None:
    """A mis-pointed checkout is a hard error, never a silent zero."""
    with pytest.raises(FileNotFoundError):
        await HandlerOccObservationSourceEffect().handle(
            ModelOccObservationSourceEffectRequest(
                checkout_dir=str(tmp_path / "does-not-exist")
            )
        )


# --------------------------------------------------------------------------- #
# Defect 3 — the composition must emit a CONCRETE streak                      #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_to_window_composition_emits_concrete_streak(
    tmp_path: Path,
) -> None:
    """source EFFECT → window COMPUTE over a real on-disk store yields N=10.

    This is the composition no workflow performs today (the three nodes are
    referenced by no workflow). Ten distinct source tuples, all clean, with a
    representative composition (>=3 merged-path, >=1 runtime/deploy-gated,
    OMN-14954) must produce ``consecutive_clean == 10`` and
    ``flip_ready is True``.
    """
    paths = (
        [EnumOccVerificationPath.MERGED_PATH] * 3
        + [EnumOccVerificationPath.RUNTIME_DEPLOY_GATED]
        + [EnumOccVerificationPath.UNSPECIFIED] * 6
    )
    records = [
        _record(1700 + i, clean=True, minute=i, path=paths[i]) for i in range(10)
    ]
    _write_store(tmp_path, records)

    source = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )
    assert source.raw_record_count == 10
    assert source.distinct_source_tuples == 10

    window = await HandlerOccAutoauthorWindow().handle(
        ModelOccAutoauthorWindowRequest(records=source.records, required_streak=10)
    )

    assert window.total_observations == 10
    assert window.distinct_tuples == 10
    assert window.consecutive_clean == 10
    assert window.merged_path_clean == 3
    assert window.runtime_gated_clean == 1
    assert window.composition_met is True
    assert window.flip_ready is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_non_clean_observation_resets_the_streak(tmp_path: Path) -> None:
    """Fail-reset is real: a non-clean observation mid-trail resets the count."""
    records = [_record(1700 + i, clean=(i != 5), minute=i) for i in range(10)]
    _write_store(tmp_path, records)

    source = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )
    window = await HandlerOccAutoauthorWindow().handle(
        ModelOccAutoauthorWindowRequest(records=source.records, required_streak=10)
    )

    assert window.total_observations == 10
    assert window.distinct_tuples == 10
    assert window.consecutive_clean == 4
    assert window.flip_ready is False
    assert window.streak_broken_by == f"{_REPO}#1705"
