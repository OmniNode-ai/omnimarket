# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607: floor accounts + token pools -> readings -> HandlerPoolUsageVerdict."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.handler_pool_usage_verdict import (
    HandlerPoolUsageVerdict,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.mapper_floor_readings import (
    readings_from_floor,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolLabelReading,
    ModelPoolUsagePolicy,
    ModelPoolUsageRequest,
    ModelPoolUsageWindowPolicy,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
POLICY = ModelPoolUsagePolicy(
    five_hour=ModelPoolUsageWindowPolicy(drain_at=0.5, limited_at=0.8),
    weekly=ModelPoolUsageWindowPolicy(drain_at=0.7, limited_at=0.9),
    max_reading_age_seconds=3600,
)


def _pool(*tokens: ModelLimitToken) -> ModelLimitTokenPool:
    return ModelLimitTokenPool(host="lab-a", source="claude", tokens=tokens)


def test_one_reading_per_account_with_the_runner_strings_parsed_or_dropped() -> None:
    accounts = (
        ModelFloorCloudAccount(
            name="alpha",
            rate_read_at="2026-12-01T11:30:00Z",
            five_hour_utilization=0.42,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.61,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
        ModelFloorCloudAccount(name="bravo"),
        ModelFloorCloudAccount(
            name="charlie",
            rate_read_at="not-a-timestamp",
            five_hour_resets_at="also-not",
            seven_day_resets_at="",
        ),
    )

    readings = readings_from_floor(accounts, ())

    assert [r.label for r in readings] == ["alpha", "bravo", "charlie"]
    alpha, bravo, charlie = readings
    assert alpha.read_at == datetime(2026, 12, 1, 11, 30, tzinfo=UTC)
    assert alpha.five_hour_utilization == 0.42
    assert alpha.five_hour_resets_at == datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    assert alpha.seven_day_utilization == 0.61
    assert alpha.seven_day_resets_at == datetime(2026, 12, 4, 0, 0, tzinfo=UTC)
    assert bravo.read_at is None
    assert bravo.five_hour_utilization is None
    assert bravo.five_hour_resets_at is None
    assert bravo.seven_day_utilization is None
    assert bravo.seven_day_resets_at is None
    assert charlie.read_at is None
    assert charlie.five_hour_resets_at is None
    assert charlie.seven_day_resets_at is None


def test_limited_until_is_the_latest_mark_the_pool_still_stands_behind() -> None:
    accounts = (ModelFloorCloudAccount(name="alpha"),)
    pools = (
        _pool(
            ModelLimitToken(label="alpha", limited_until="2026-12-01T13:00:00Z"),
            ModelLimitToken(
                label="alpha",
                limited_until="2026-12-01T18:00:00Z",
                revoked=True,
            ),
            ModelLimitToken(label="other", limited_until="2026-12-02T00:00:00Z"),
            ModelLimitToken(label="alpha", limited_until=""),
            ModelLimitToken(label="alpha", limited_until="unparsable"),
        ),
    )

    (reading,) = readings_from_floor(accounts, pools)

    assert reading.limited_until == datetime(2026, 12, 1, 13, 0, tzinfo=UTC)
    # one host's revoked login is not the account's: the label stays unrevoked while another token of it is live
    assert reading.revoked is False


def test_no_surviving_mark_and_revoked_needs_a_token() -> None:
    accounts = (ModelFloorCloudAccount(name="alpha"),)
    pools = (
        _pool(
            ModelLimitToken(
                label="alpha", limited_until="2026-12-01T18:00:00Z", revoked=True
            ),
        ),
    )

    (reading,) = readings_from_floor(accounts, pools)

    assert reading.limited_until is None
    assert reading.revoked is True
    (empty,) = readings_from_floor(accounts, ())
    assert empty.limited_until is None
    assert empty.revoked is False


def test_golden_chain_floor_rows_to_verdicts() -> None:
    accounts = (
        ModelFloorCloudAccount(
            name="alpha",
            rate_read_at="2026-12-01T11:30:00Z",
            five_hour_utilization=0.42,
            five_hour_resets_at="2026-12-01T15:00:00Z",
            seven_day_utilization=0.61,
            seven_day_resets_at="2026-12-04T00:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="drainy",
            rate_read_at="2026-12-01T11:00:00Z",
            five_hour_utilization=0.55,
            five_hour_resets_at="2026-12-01T14:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="stale",
            rate_read_at="2026-12-01T06:00:00Z",
            five_hour_utilization=0.1,
            five_hour_resets_at="2026-12-01T15:00:00Z",
        ),
        ModelFloorCloudAccount(
            name="ok-one",
            rate_read_at="2026-12-01T11:00:00Z",
            five_hour_utilization=0.1,
            five_hour_resets_at="2026-12-01T15:00:00Z",
        ),
    )
    pools = (_pool(ModelLimitToken(label="alpha", revoked=True)),)
    request = ModelPoolUsageRequest(
        now=NOW,
        policy=POLICY,
        readings=readings_from_floor(accounts, pools),
        correlation_id=uuid4(),
    )

    result = HandlerPoolUsageVerdict().handle(request)

    verdicts = {u.label: (u.verdict, u.reason) for u in result.labels}
    assert verdicts["alpha"] == ("limited", "revoked")
    assert verdicts["drainy"] == ("drain", "five_hour_drain")
    assert verdicts["stale"] == ("unknown", "stale_reading")
    assert result.drain_labels == ("drainy",)


def test_the_mapper_is_deterministic_and_pure_of_clock() -> None:
    accounts = (
        ModelFloorCloudAccount(name="alpha", rate_read_at="2026-12-01T11:30:00Z"),
    )
    pools = (
        _pool(ModelLimitToken(label="alpha", limited_until="2026-12-02T00:00:00Z")),
    )

    first: tuple[ModelPoolLabelReading, ...] = readings_from_floor(accounts, pools)
    second = readings_from_floor(accounts, pools)

    assert first == second
