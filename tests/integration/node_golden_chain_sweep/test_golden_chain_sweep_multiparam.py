# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_golden_chain_sweep (OMN-13683, WS-5 Wave 9).

Variant A (COMPUTE): drives the real ``NodeGoldenChainSweep.handle`` over a matrix
of chain definitions + caller-supplied ``projected_rows`` + ``idle_gate`` +
injected ``now_iso`` reference clock. The node is pure (zero live I/O — no Kafka,
no DB), so this runs fully in-memory; we always pass an explicit ``chains`` list so
the packaged-registry default is never triggered.

Asserts the full tri-state surface and typed counts: per-chain
``EnumChainStatus`` (PASS / FAIL / TIMEOUT / GATED / STALE / ERROR), the
``missing_fields`` / ``row_age_seconds`` finding structures, the rolled-up
``EnumSweepStatus`` (PASS / PARTIAL / FAIL / GATED / WARN), and the
``chains_passed/failed/gated/stale`` counters. Negative controls: a missing field
produces a FAIL with the missing field named; a weeks-old row produces STALE; an
empty chain set is fail-closed FAIL (vacuous truth is not health).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)

_NOW = "2026-06-21T12:00:00+00:00"
_FRESH_TS = "2026-06-21T11:59:30+00:00"  # 30s old
_STALE_TS = "2026-05-01T00:00:00+00:00"  # weeks old


def _chain(
    name: str,
    *,
    expected_fields: list[str] | None = None,
    max_row_age_seconds: int | None = None,
    timestamp_field: str = "created_at",
) -> ModelChainDefinition:
    return ModelChainDefinition(
        name=name,
        head_topic=f"onex.evt.{name}.v1",
        tail_table=f"tbl_{name}",
        expected_fields=expected_fields or ["correlation_id", "payload"],
        max_row_age_seconds=max_row_age_seconds,
        timestamp_field=timestamp_field,
    )


def _req(
    chains: list[ModelChainDefinition],
    rows: dict[str, dict[str, object]],
    *,
    idle_gate: bool = False,
    now_iso: str | None = None,
) -> GoldenChainSweepRequest:
    return GoldenChainSweepRequest(
        chains=chains,
        projected_rows=rows,
        idle_gate=idle_gate,
        now_iso=now_iso,
    )


# id -> builder returning (request, expected dict)
def _c_single_pass() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req(
        [_chain("c1")],
        {"c1": {"correlation_id": "x", "payload": {}}},
    )
    return req, {
        "overall": EnumSweepStatus.PASS,
        "passed": 1,
        "failed": 0,
        "gated": 0,
        "stale": 0,
        "chain0_status": EnumChainStatus.PASS,
    }


def _c_missing_field_fail() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req([_chain("c1")], {"c1": {"correlation_id": "x"}})
    return req, {
        "overall": EnumSweepStatus.FAIL,
        "passed": 0,
        "failed": 1,
        "gated": 0,
        "stale": 0,
        "chain0_status": EnumChainStatus.FAIL,
        "chain0_missing": ["payload"],
    }


def _c_no_row_timeout() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req([_chain("c1")], {})
    return req, {
        "overall": EnumSweepStatus.FAIL,
        "passed": 0,
        "failed": 1,
        "gated": 0,
        "stale": 0,
        "chain0_status": EnumChainStatus.TIMEOUT,
    }


def _c_no_row_gated() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req([_chain("c1")], {}, idle_gate=True)
    return req, {
        "overall": EnumSweepStatus.GATED,
        "passed": 0,
        "failed": 0,
        "gated": 1,
        "stale": 0,
        "chain0_status": EnumChainStatus.GATED,
    }


def _c_stale_row_warn() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req(
        [_chain("c1", max_row_age_seconds=60)],
        {"c1": {"correlation_id": "x", "payload": {}, "created_at": _STALE_TS}},
        now_iso=_NOW,
    )
    return req, {
        "overall": EnumSweepStatus.WARN,
        "passed": 0,
        "failed": 0,
        "gated": 0,
        "stale": 1,
        "chain0_status": EnumChainStatus.STALE,
    }


def _c_fresh_row_pass() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req(
        [_chain("c1", max_row_age_seconds=60)],
        {"c1": {"correlation_id": "x", "payload": {}, "created_at": _FRESH_TS}},
        now_iso=_NOW,
    )
    return req, {
        "overall": EnumSweepStatus.PASS,
        "passed": 1,
        "failed": 0,
        "gated": 0,
        "stale": 0,
        "chain0_status": EnumChainStatus.PASS,
    }


def _c_freshness_no_clock_error() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    # max_row_age set but now_iso missing → ERROR (fail-fast, not silent pass).
    req = _req(
        [_chain("c1", max_row_age_seconds=60)],
        {"c1": {"correlation_id": "x", "payload": {}, "created_at": _FRESH_TS}},
        now_iso=None,
    )
    return req, {
        "overall": EnumSweepStatus.FAIL,
        "passed": 0,
        "failed": 1,
        "gated": 0,
        "stale": 0,
        "chain0_status": EnumChainStatus.ERROR,
    }


def _c_mixed_partial() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req(
        [_chain("c1"), _chain("c2")],
        {
            "c1": {"correlation_id": "x", "payload": {}},
            "c2": {"correlation_id": "y"},  # missing payload → FAIL
        },
    )
    return req, {
        "overall": EnumSweepStatus.PARTIAL,
        "passed": 1,
        "failed": 1,
        "gated": 0,
        "stale": 0,
    }


def _c_empty_chains_fail_closed() -> tuple[GoldenChainSweepRequest, dict[str, object]]:
    req = _req([], {})
    return req, {
        "overall": EnumSweepStatus.FAIL,
        "passed": 0,
        "failed": 0,
        "gated": 0,
        "stale": 0,
        "total": 0,
    }


_CASES: list[
    tuple[str, Callable[[], tuple[GoldenChainSweepRequest, dict[str, object]]]]
] = [
    ("single-pass", _c_single_pass),
    ("missing-field-fail-negative-control", _c_missing_field_fail),
    ("no-row-timeout-fail", _c_no_row_timeout),
    ("no-row-idle-gated", _c_no_row_gated),
    ("stale-row-warn", _c_stale_row_warn),
    ("fresh-row-pass", _c_fresh_row_pass),
    ("freshness-no-clock-error", _c_freshness_no_clock_error),
    ("mixed-pass-and-fail-partial", _c_mixed_partial),
    ("empty-chains-fail-closed", _c_empty_chains_fail_closed),
]


@pytest.mark.integration
@pytest.mark.parametrize("builder", [c[1] for c in _CASES], ids=[c[0] for c in _CASES])
def test_golden_chain_sweep_multiparam(
    builder: Callable[[], tuple[GoldenChainSweepRequest, dict[str, object]]],
) -> None:
    req, exp = builder()
    result = NodeGoldenChainSweep().handle(req)

    overall = exp["overall"]
    assert isinstance(overall, EnumSweepStatus)
    assert result.overall_status == overall
    assert result.status == overall.value
    assert result.chains_passed == exp["passed"]
    assert result.chains_failed == exp["failed"]
    assert result.chains_gated == exp["gated"]
    assert result.chains_stale == exp["stale"]

    if "total" in exp:
        assert result.chains_total == exp["total"]
    else:
        assert result.chains_total == len(req.chains)
        assert len(result.chain_results) == len(req.chains)

    if "chain0_status" in exp:
        assert result.chain_results[0].status == exp["chain0_status"]
    if "chain0_missing" in exp:
        assert result.chain_results[0].missing_fields == exp["chain0_missing"]

    # Count integrity: every per-chain result is bucketed into exactly one counter.
    bucketed = (
        result.chains_passed
        + result.chains_failed
        + result.chains_gated
        + result.chains_stale
    )
    assert bucketed == len(result.chain_results)


def test_negative_control_missing_field_names_the_gap() -> None:
    """A field-incomplete tail row MUST fail and name the missing field — never a
    silent pass."""
    result = NodeGoldenChainSweep().handle(
        _req(
            [_chain("c1", expected_fields=["a", "b", "c"])],
            {"c1": {"a": 1}},
        )
    )
    assert result.overall_status == EnumSweepStatus.FAIL
    assert result.chain_results[0].status == EnumChainStatus.FAIL
    assert result.chain_results[0].missing_fields == ["b", "c"]


def test_negative_control_stale_row_reports_age() -> None:
    """A weeks-old row with all fields present is STALE (WARN), and the age is
    surfaced — field presence alone is not proof of recent flow."""
    result = NodeGoldenChainSweep().handle(
        _req(
            [_chain("c1", max_row_age_seconds=60)],
            {"c1": {"correlation_id": "x", "payload": {}, "created_at": _STALE_TS}},
            now_iso=_NOW,
        )
    )
    assert result.overall_status == EnumSweepStatus.WARN
    cr = result.chain_results[0]
    assert cr.status == EnumChainStatus.STALE
    assert cr.row_age_seconds is not None
    assert cr.row_age_seconds > 60
