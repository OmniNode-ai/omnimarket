# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607 SEAM 1: usage panel rows -> readings_from_floor -> verdict handler."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

import omnimarket.nodes.node_pool_usage_verdict_compute as node_package
from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.mapper_floor_readings import (
    readings_from_floor,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolLabelUsage,
    ModelPoolUsagePolicy,
    ModelPoolUsageRequest,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
NOW = datetime(2026, 12, 1, 12, 0, 0, tzinfo=UTC)
# The fields the seam reads on the floor side; a rename there breaks this test,
# not a silent None on every reading.
ACCOUNT_FIELDS = (
    "name",
    "rate_read_at",
    "five_hour_utilization",
    "five_hour_resets_at",
    "seven_day_utilization",
    "seven_day_resets_at",
)
TOKEN_FIELDS = ("label", "limited_until", "revoked")


def _policy() -> ModelPoolUsagePolicy:
    return ModelPoolUsagePolicy.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )


def _accounts() -> tuple[ModelFloorCloudAccount, ...]:
    return (
        ModelFloorCloudAccount(
            name="alpha",
            rate_read_at="2026-12-01T11:57:00Z",
            five_hour_utilization=0.42,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.55,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="bravo",
            rate_read_at="2026-12-01T11:58:00Z",
            five_hour_utilization=0.95,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.50,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="charlie",
            rate_read_at="",
            five_hour_utilization=0.30,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.40,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="delta",
            rate_read_at="2026-12-01T11:59:00Z",
            five_hour_utilization=0.10,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.20,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
    )


def _pools() -> tuple[ModelLimitTokenPool, ...]:
    return (
        ModelLimitTokenPool(
            host="lab-1",
            read_at="2026-12-01T11:59:00Z",
            source="pool-status-mirror",
            tokens=(
                ModelLimitToken(label="alpha"),
                ModelLimitToken(label="delta", limited_until="2026-12-01T13:00:00Z"),
            ),
        ),
    )


def _verdicts() -> dict[str, ModelPoolLabelUsage]:
    readings = readings_from_floor(_accounts(), _pools())
    result = node_package.NodePoolUsageVerdictCompute().handle(
        ModelPoolUsageRequest(
            now=NOW,
            policy=_policy(),
            readings=readings,
            correlation_id=UUID("00000000-0000-4000-8000-000000000607"),
        )
    )
    assert result.ok == 1
    assert result.drain == 1
    assert result.limited == 1
    assert result.unknown == 1
    assert result.drain_labels == ("bravo",)
    return {usage.label: usage for usage in result.labels}


def test_mapper_reads_only_fields_the_floor_models_still_carry() -> None:
    for field in ACCOUNT_FIELDS:
        assert field in ModelFloorCloudAccount.model_fields
    for field in TOKEN_FIELDS:
        assert field in ModelLimitToken.model_fields
    assert "tokens" in ModelLimitTokenPool.model_fields


def test_seam_floor_rows_to_verdicts() -> None:
    readings = readings_from_floor(_accounts(), _pools())
    assert tuple(r.label for r in readings) == ("alpha", "bravo", "charlie", "delta")
    assert readings[0].read_at == datetime(2026, 12, 1, 11, 57, 0, tzinfo=UTC)
    assert readings[0].five_hour_utilization == 0.42
    assert readings[3].limited_until == datetime(2026, 12, 1, 13, 0, 0, tzinfo=UTC)

    by_label = _verdicts()
    alpha = by_label["alpha"]
    assert alpha.verdict == "ok"
    assert alpha.reason == "within_thresholds"
    assert alpha.five_hour.state == "ok"
    assert alpha.five_hour.fraction_used == 0.42
    assert alpha.weekly.state == "ok"

    bravo = by_label["bravo"]
    assert bravo.verdict == "drain"
    assert bravo.reason == "five_hour_drain"
    assert bravo.five_hour.state == "drain"
    assert bravo.five_hour.seconds_to_reset == 10800
    assert bravo.weekly.state == "ok"

    delta = by_label["delta"]
    assert delta.verdict == "limited"
    assert delta.reason == "pool_limited_mark"
    assert delta.seconds_until_usable == 3600


def test_a_never_read_account_is_never_ok() -> None:
    by_label = _verdicts()
    charlie = by_label["charlie"]
    assert charlie.verdict == "unknown"
    assert charlie.reason == "no_reading"
