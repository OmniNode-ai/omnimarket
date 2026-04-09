# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for NodePlatformReadinessV2 orchestrator — OMN-8141."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_platform_readiness.handlers.dimension_checks import (
    CheckContext,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2 import (
    NodePlatformReadinessV2,
    _worst_status,
    _write_yaml_artifact,
)
from omnimarket.nodes.node_platform_readiness.models.dimension_result_v2 import (
    ModelDimensionResultV2,
)


def _make_dim(dimension: str, status: EnumReadinessStatus) -> ModelDimensionResultV2:
    return ModelDimensionResultV2(
        dimension=dimension,
        status=status,
        check_count=1,
        evidence_source="mock",
    )


ALL_PASS = [_make_dim(f"dim_{i}", EnumReadinessStatus.PASS) for i in range(7)]
ALL_WARN = [_make_dim(f"dim_{i}", EnumReadinessStatus.WARN) for i in range(7)]
ONE_FAIL = [
    *[_make_dim(f"dim_{i}", EnumReadinessStatus.PASS) for i in range(6)],
    _make_dim("dim_6", EnumReadinessStatus.FAIL),
]
ONE_WARN = [
    *[_make_dim(f"dim_{i}", EnumReadinessStatus.PASS) for i in range(6)],
    _make_dim("dim_6", EnumReadinessStatus.WARN),
]


# ---------------------------------------------------------------------------
# _worst_status helper
# ---------------------------------------------------------------------------


def test_worst_status_all_pass() -> None:
    assert _worst_status([EnumReadinessStatus.PASS] * 7) == EnumReadinessStatus.PASS


def test_worst_status_one_warn() -> None:
    assert (
        _worst_status(
            [
                EnumReadinessStatus.PASS,
                EnumReadinessStatus.WARN,
                EnumReadinessStatus.PASS,
            ]
        )
        == EnumReadinessStatus.WARN
    )


def test_worst_status_fail_beats_warn() -> None:
    assert (
        _worst_status(
            [
                EnumReadinessStatus.PASS,
                EnumReadinessStatus.WARN,
                EnumReadinessStatus.FAIL,
            ]
        )
        == EnumReadinessStatus.FAIL
    )


# ---------------------------------------------------------------------------
# _write_yaml_artifact
# ---------------------------------------------------------------------------


def test_write_yaml_artifact_creates_latest(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    latest = _write_yaml_artifact(tmp_path, EnumReadinessStatus.PASS, ALL_PASS, now)

    assert latest.exists()
    content = yaml.safe_load(latest.read_text())
    assert content["overall_status"] == "PASS"
    assert len(content["dimensions"]) == 7


def test_write_yaml_artifact_creates_snapshot(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    _write_yaml_artifact(tmp_path, EnumReadinessStatus.WARN, ALL_WARN, now)

    snapshots = list(tmp_path.glob("snapshot-*.yaml"))
    assert len(snapshots) == 1
    content = yaml.safe_load(snapshots[0].read_text())
    assert content["overall_status"] == "WARN"


def test_write_yaml_artifact_retention(tmp_path: Path) -> None:
    """Only the last 30 snapshots are kept."""
    from datetime import UTC, datetime, timedelta

    base_time = datetime.now(UTC)
    for i in range(35):
        t = base_time - timedelta(minutes=i)
        _write_yaml_artifact(tmp_path, EnumReadinessStatus.PASS, ALL_PASS, t)

    snapshots = list(tmp_path.glob("snapshot-*.yaml"))
    assert len(snapshots) <= 30


def test_write_yaml_artifact_all_7_dims(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    dims = [_make_dim(f"d{i}", EnumReadinessStatus.PASS) for i in range(7)]
    _write_yaml_artifact(tmp_path, EnumReadinessStatus.PASS, dims, datetime.now(UTC))

    content = yaml.safe_load((tmp_path / "latest.yaml").read_text())
    assert len(content["dimensions"]) == 7


# ---------------------------------------------------------------------------
# NodePlatformReadinessV2.handle
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator(tmp_path: Path) -> NodePlatformReadinessV2:
    return NodePlatformReadinessV2(omni_home=str(tmp_path))


def test_orchestrator_all_pass(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """Overall PASS when all dimensions pass."""
    ctx = CheckContext(omni_home=tmp_path)

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
        return_value=ALL_PASS,
    ):
        result = asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    assert result.overall == EnumReadinessStatus.PASS
    assert result.blockers == []
    assert result.degraded == []


def test_orchestrator_one_fail(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """Overall FAIL when any dimension fails."""
    ctx = CheckContext(omni_home=tmp_path)
    dims = [*ALL_PASS[:6], _make_dim("runtime_wiring", EnumReadinessStatus.FAIL)]
    # Add actionable item to the FAIL dimension
    dims[-1] = ModelDimensionResultV2(
        dimension="runtime_wiring",
        status=EnumReadinessStatus.FAIL,
        check_count=0,
        evidence_source="mock",
        actionable_items=["only 5 nodes registered"],
    )

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
        return_value=dims,
    ):
        result = asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    assert result.overall == EnumReadinessStatus.FAIL
    assert len(result.blockers) == 1
    assert "runtime_wiring" in result.blockers[0]


def test_orchestrator_one_warn(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """Overall WARN when worst is WARN."""
    ctx = CheckContext(omni_home=tmp_path)
    dims = [*ALL_PASS[:6], _make_dim("ci_health", EnumReadinessStatus.WARN)]

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
        return_value=dims,
    ):
        result = asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    assert result.overall == EnumReadinessStatus.WARN
    assert len(result.degraded) == 1


def test_orchestrator_writes_artifact(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """YAML artifact written after run."""
    ctx = CheckContext(omni_home=tmp_path)

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
        return_value=ALL_PASS,
    ):
        asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    latest = tmp_path / ".onex_state" / "readiness" / "latest.yaml"
    assert latest.exists()
    content = yaml.safe_load(latest.read_text())
    assert content["overall_status"] == "PASS"
    assert len(content["dimensions"]) == 7


def test_orchestrator_returns_v1_compatible_result(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """Returns ModelPlatformReadinessResult (V1 type) with 7 dimensions."""
    ctx = CheckContext(omni_home=tmp_path)

    with patch(
        "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
        return_value=ALL_PASS,
    ):
        result = asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    assert len(result.dimensions) == 7
    assert all(
        hasattr(d, "name") and hasattr(d, "freshness") for d in result.dimensions
    )


def test_orchestrator_kafka_failure_does_not_crash(
    orchestrator: NodePlatformReadinessV2, tmp_path: Path
) -> None:
    """Kafka emit failure is best-effort — does not abort the run."""
    ctx = CheckContext(omni_home=tmp_path)

    with (
        patch(
            "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2.run_all_dimensions",
            return_value=ALL_PASS,
        ),
        patch(
            "omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness_v2._emit_kafka_event",
            side_effect=RuntimeError("kafka down"),
        ),
    ):
        # Should not raise
        result = asyncio.get_event_loop().run_until_complete(orchestrator.handle(ctx))

    assert result.overall == EnumReadinessStatus.PASS
