# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reading source that declares ``unknown_when`` reads UNKNOWN for that row, never NOT WORKING (OMN-23655).

Seam test for piece 11: the dev deploy reading writes a row with ``verdict=UNKNOWN`` when a merge is queued behind the deploy
agent, and the reading writer's piece 11 source (overlay ``unknown_when``) must read it UNKNOWN. The source here is the same
shape the overlays repository holds for ``dev_deploy_reading``. Each test names the edit that turns it red.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelCellPredicate,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSource,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    CUTOFF,
    END,
    START,
    stamp,
)

pytestmark = pytest.mark.unit

MAX_AGE = 7200


def _source(*, with_unknown: bool) -> ModelReadingSource:
    return ModelReadingSource(
        source_id="dev_deploy_reading",
        row_type="STATUS",
        lane="dev-deploy-reading",
        cells={"src": "dev-deploy-reading", "drill": "no"},
        mode="newest",
        unknown_when=(
            (ModelCellPredicate(cell="verdict", op="eq", value="UNKNOWN"),)
            if with_unknown
            else ()
        ),
        pass_when=(ModelCellPredicate(cell="verdict", op="eq", value="WORKING"),),
        max_age_seconds=MAX_AGE,
    )


def _read(verdict: str, *, with_unknown: bool = True, age_minutes: int = 20):
    at = END - timedelta(minutes=age_minutes)
    row = (
        f"{stamp(at)} | STATUS | lane=dev-deploy-reading | src=dev-deploy-reading | drill=no"
        f" | verdict={verdict} | req=r-{verdict} | reason=deploy-queued:aaaaaaaaaaaa:ahead-2 | row"
    )
    parsed = evidence_row_from_text(row, None)
    assert parsed is not None
    spec = ModelPieceReadingSpec(
        piece=11,
        name="lab deploy receipts",
        readable_today=True,
        sources=(_source(with_unknown=with_unknown),),
        injected_fault="No PASS receipt within an hour",
    )
    return HandlerReadingCompute().handle(
        ModelReadingComputeRequest(
            spec=spec,
            window_start=START,
            window_end=END,
            cutoff=CUTOFF,
            order_hash="order-hash",
            overlay_hash="overlay-hash",
            revision="1.0.0",
            sources=(
                ModelReadingSourceRows(
                    source_id="dev_deploy_reading", rows=(parsed,), error=None
                ),
            ),
        )
    )


def test_an_unknown_row_reads_unknown_with_the_source_named() -> None:
    result = _read("UNKNOWN")
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.READ
    assert result.reasons == ("source-unknown:dev_deploy_reading",)


def test_control_without_unknown_when_the_same_row_reads_not_working() -> None:
    """The reading the piece had before this change: the failing control for the test above."""
    result = _read("UNKNOWN", with_unknown=False)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons == ("source-fail:dev_deploy_reading",)


def test_a_working_row_still_reads_working_and_a_not_working_row_still_fails() -> None:
    assert _read("WORKING").verdict is EnumReadingVerdict.WORKING
    failed = _read("NOT_WORKING")
    assert failed.verdict is EnumReadingVerdict.NOT_WORKING
    assert failed.reasons == ("source-fail:dev_deploy_reading",)


def test_error_chain_a_stale_unknown_row_is_a_failure_not_an_unknown() -> None:
    stale = _read("UNKNOWN", age_minutes=MAX_AGE // 60 + 30)
    assert stale.verdict is EnumReadingVerdict.NOT_WORKING
    assert stale.reasons[0].startswith("stale-source:dev_deploy_reading:")
