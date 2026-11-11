# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a fixed request of seven tasks is answered with the exact census."""

from __future__ import annotations

import pytest

from omnimarket.models.code_eval_suite.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)
from omnimarket.models.code_eval_suite.model_suite_census_result import (
    ModelCodeEvalSuiteCensus,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from omnimarket.ranges.compare import required_comparison_size
from omnimarket.ranges.power import required_sample_size
from tests.nodes.node_code_eval_suite_census_compute.support import (
    FAIL,
    PASS,
    overlay,
    task,
)

pytestmark = pytest.mark.unit

S = EnumSizeBand.S
M = EnumSizeBand.M
L = EnumSizeBand.L


def _request() -> ModelCodeEvalSuiteCensusRequest:
    return ModelCodeEvalSuiteCensusRequest(
        suite_id="suite-golden",
        tasks=(
            task("repo-a", 1, "OMN-1", S, PASS),
            task("repo-a", 2, "OMN-2", S, PASS),
            task("repo-a", 3, "OMN-3", M, PASS),
            task("repo-b", 4, "OMN-4", M, PASS),
            task("repo-b", 5, "OMN-5", L, PASS),
            task("repo-b", 6, "OMN-6", L, PASS),
            task("repo-a", 7, "OMN-7", M, FAIL),
        ),
    )


def _handle(req: ModelCodeEvalSuiteCensusRequest) -> ModelCodeEvalSuiteCensus:
    return HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(req)


def test_golden_chain_candidates_are_exact() -> None:
    result = _handle(_request())
    assert result.failure is None
    assert result.candidates_total == 7
    assert [
        (c.repository, c.merge_commit, c.contract_id) for c in result.candidates
    ] == [
        ("repo-a", "0000000000000000000000000000000000000001", "OMN-1"),
        ("repo-a", "0000000000000000000000000000000000000002", "OMN-2"),
        ("repo-a", "0000000000000000000000000000000000000003", "OMN-3"),
        ("repo-b", "0000000000000000000000000000000000000004", "OMN-4"),
        ("repo-b", "0000000000000000000000000000000000000005", "OMN-5"),
        ("repo-b", "0000000000000000000000000000000000000006", "OMN-6"),
    ]


def test_golden_chain_by_band_admitted_counts() -> None:
    result = _handle(_request())
    line = overlay().line
    required_cell_n = required_sample_size(
        floor=line.floor,
        margin=line.margin,
        confidence=line.confidence,
        power=line.power,
    )
    required_paired_n = required_comparison_size(
        margin=line.margin, confidence=line.confidence, power=line.power
    )
    by_band = {b.band: b for b in result.by_band}
    assert set(by_band) == {S, M, L}
    assert by_band[S].admitted == 2
    assert by_band[M].admitted == 2
    assert by_band[L].admitted == 2
    for b in result.by_band:
        assert b.required_cell_n == required_cell_n
        assert b.required_paired_n == required_paired_n
        assert b.cell_shortfall == max(0, required_cell_n - b.admitted)
        assert b.paired_shortfall == max(0, required_paired_n - b.admitted)


def test_golden_chain_by_repository_and_band_repository() -> None:
    result = _handle(_request())
    by_repo = {r.repository: r.admitted for r in result.by_repository}
    assert by_repo == {"repo-a": 3, "repo-b": 3}
    pairs = {(b.band, b.repository): b.admitted for b in result.by_band_repository}
    assert pairs == {
        (S, "repo-a"): 2,
        (M, "repo-a"): 1,
        (M, "repo-b"): 1,
        (L, "repo-b"): 2,
    }


def test_golden_chain_excluded_rows_and_control_counts() -> None:
    result = _handle(_request())
    assert [(e.reason, e.count) for e in result.excluded] == [("control:fail_check", 1)]
    assert result.control is not None
    assert (
        result.control.holds,
        result.control.does_not_hold,
        result.control.could_not_run,
    ) == (
        6,
        1,
        0,
    )


def test_golden_chain_is_deterministic_and_digests_match() -> None:
    first = _handle(_request())
    second = _handle(_request())
    assert first == second
    assert first.suite_digest == second.suite_digest
    assert first.suite_digest
