# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_platform_readiness (OMN-13676).

COMPUTE node, Variant A. The handler self-collects via subprocess/ssh ONLY when
``dimensions`` is empty. We exercise the pure aggregation path by supplying typed
synthetic ``ModelDimensionInput`` rows and an injected ``now`` for deterministic
freshness math — no subprocess, no ssh, no monkeypatching. Each case asserts the
typed tri/quad-state result (overall, per-dimension status, blockers/degraded);
the negative-control cases force FAIL and GATED dimensions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
    ModelDimensionInput,
    ModelPlatformReadinessRequest,
    NodePlatformReadiness,
)

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
# A flag name that is intentionally absent from the environment so the gated
# dimension resolves to GATED (pipeline wired, flag off).
_DISABLED_FLAG = "ENABLE_OMN13676_NONEXISTENT_FLAG"


def _fresh(name: str, *, healthy: bool, critical: bool = False) -> ModelDimensionInput:
    return ModelDimensionInput(
        name=name,
        critical=critical,
        healthy=healthy,
        last_checked=_NOW,
        details=f"{name} fresh",
    )


# (id, dimensions, expected_overall, expected_blockers, expected_degraded)
CASES = [
    pytest.param(
        [_fresh("a", healthy=True), _fresh("b", healthy=True)],
        EnumReadinessStatus.PASS,
        0,
        0,
        id="all-pass",
    ),
    pytest.param(
        [
            _fresh("a", healthy=True),
            ModelDimensionInput(
                name="stale_dim",
                healthy=True,
                last_checked=_NOW - timedelta(hours=30),  # > 24h → WARN
                details="stale data",
            ),
        ],
        EnumReadinessStatus.WARN,
        0,
        1,
        id="one-warn-stale",
    ),
    pytest.param(
        [
            _fresh("a", healthy=True),
            ModelDimensionInput(
                name="mock_dim",
                critical=True,
                healthy=True,
                last_checked=_NOW,
                is_mock=True,  # mock → FAIL
                details="mock data",
            ),
        ],
        EnumReadinessStatus.FAIL,
        1,
        0,
        id="one-fail-mock",
    ),
    pytest.param(
        [
            _fresh("pass_dim", healthy=True),
            ModelDimensionInput(
                name="warn_dim",
                healthy=True,
                last_checked=_NOW - timedelta(hours=30),  # WARN
            ),
            ModelDimensionInput(
                name="fail_dim",
                critical=True,
                healthy=None,  # missing → FAIL
                last_checked=None,
            ),
        ],
        EnumReadinessStatus.FAIL,
        1,
        1,
        id="mixed-tri-state",
    ),
    pytest.param(
        [
            _fresh("a", healthy=True),
            ModelDimensionInput(
                name="gated_dim",
                healthy=True,
                last_checked=_NOW,
                gated_by_flag=_DISABLED_FLAG,  # flag off → GATED (degraded, not blocker)
            ),
        ],
        EnumReadinessStatus.WARN,
        0,
        1,
        id="gated-by-disabled-flag",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("dimensions", "expected_overall", "expected_blockers", "expected_degraded"),
    [(c.values[0], c.values[1], c.values[2], c.values[3]) for c in CASES],
    ids=[c.id for c in CASES],
)
def test_platform_readiness_multiparam(
    monkeypatch: pytest.MonkeyPatch,
    dimensions: list[ModelDimensionInput],
    expected_overall: EnumReadinessStatus,
    expected_blockers: int,
    expected_degraded: int,
) -> None:
    # Ensure the gated flag is genuinely absent (env-independent determinism).
    monkeypatch.delenv(_DISABLED_FLAG, raising=False)

    result = NodePlatformReadiness().handle(
        ModelPlatformReadinessRequest(dimensions=dimensions, now=_NOW)
    )

    assert result.overall == expected_overall
    assert len(result.dimensions) == len(dimensions)
    assert len(result.blockers) == expected_blockers
    assert len(result.degraded) == expected_degraded
    # Every dimension carries a typed quad-state status (real evaluation).
    assert all(
        d.status
        in {
            EnumReadinessStatus.PASS,
            EnumReadinessStatus.WARN,
            EnumReadinessStatus.FAIL,
            EnumReadinessStatus.GATED,
        }
        for d in result.dimensions
    )
    assert result.timestamp == _NOW


@pytest.mark.integration
def test_platform_readiness_gated_status_is_distinct_from_fail() -> None:
    """A gated dimension must resolve to GATED, never FAIL (yellow, not red)."""
    result = NodePlatformReadiness().handle(
        ModelPlatformReadinessRequest(
            dimensions=[
                ModelDimensionInput(
                    name="gated_only",
                    healthy=True,
                    last_checked=_NOW,
                    gated_by_flag=_DISABLED_FLAG,
                )
            ],
            now=_NOW,
        )
    )
    gated = result.dimensions[0]
    assert gated.status == EnumReadinessStatus.GATED
    assert gated.freshness == "gated"
    assert result.overall == EnumReadinessStatus.WARN
    assert result.blockers == []
