# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor record carries a ``limits`` section: one row per account, key, engine, rung and token pool.

Every row names its source and read time. A row never read, or read longer ago than one publish cycle, says
UNKNOWN and carries no number at all: a stale or missing reading is never drawn as zero. A revoked token is
counted as revoked and never as capacity.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelFloorLimitsFacts,
    ModelLimitApiKey,
    ModelLimitEngine,
    ModelLimitLabRung,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorLimitRow,
    ModelFloorLimits,
)
from tests.nodes.node_factory_floor_compute.support import NOW, config, request


def stamp(minutes_ago: float) -> str:
    return f"{NOW - timedelta(minutes=minutes_ago):%Y-%m-%dT%H:%M:%SZ}"


def limits_of(**facts: Any) -> ModelFloorLimits:
    """Run the handler exactly as the effect node does and return the record's limits section."""
    result = HandlerFactoryFloor().handle(
        request(limits=ModelFloorLimitsFacts(**facts))
    )
    return result.limits


def rows_of(limits: ModelFloorLimits, kind: str) -> list[ModelFloorLimitRow]:
    return [r for r in limits.rows if r.kind == kind]


def account(**over: Any) -> ModelFloorCloudAccount:
    base: dict[str, Any] = {
        "name": "acct-a",
        "login": "ok",
        "rate_read_at": stamp(1),
        "five_hour_utilization": 0.42,
        "five_hour_resets_at": "2026-10-31T15:00:00Z",
        "seven_day_utilization": 0.1,
        "seven_day_resets_at": "2026-11-04T00:00:00Z",
        "credit_remaining": 25.5,
        "credit_read_at": stamp(2),
        "credit_source": "claude.ai/settings/usage",
    }
    base.update(over)
    return ModelFloorCloudAccount(**base)


def with_accounts(*accounts: ModelFloorCloudAccount, **facts: Any) -> ModelFloorLimits:
    result = HandlerFactoryFloor().handle(
        request(cloud_accounts=accounts, limits=ModelFloorLimitsFacts(**facts))
    )
    return result.limits


def numbers_in(value: Any) -> list[float]:
    """Every number in a JSON-shaped value (booleans are not numbers)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in numbers_in(v)]
    return []


def test_limits_contract_declares_the_cycle_the_budget_and_the_expiry() -> None:
    cfg = config().limits
    assert cfg.publish_cycle_minutes > 0
    assert cfg.api_budget_usd == 200
    assert cfg.cloud_credit_expiry == "2026-11-05"


def test_limits_claude_subscription_row_carries_both_windows_and_the_pool_limit() -> (
    None
):
    pool = ModelLimitTokenPool(
        host="h200",
        read_at=stamp(1),
        source="claude-token-pool.json",
        tokens=(ModelLimitToken(label="acct-a", limited_until="2026-10-31T13:30:00Z"),),
    )
    limits = with_accounts(account(), token_pools=(pool,))
    (row,) = rows_of(limits, "claude_subscription")
    assert row.name == "acct-a"
    assert row.state == "read"
    assert row.source == "cloud-accounts.json rate"
    assert row.read_at == stamp(1)
    assert row.values["five_hour_utilization"] == pytest.approx(0.42)
    assert row.values["five_hour_resets_at"] == "2026-10-31T15:00:00Z"
    assert row.values["seven_day_utilization"] == pytest.approx(0.1)
    assert row.values["seven_day_resets_at"] == "2026-11-04T00:00:00Z"
    assert row.values["limited_until"] == "2026-10-31T13:30:00Z"


def test_limits_claude_subscription_without_a_rate_reading_is_unknown_for_that_field() -> (
    None
):
    limits = with_accounts(account(seven_day_utilization=None))
    (row,) = rows_of(limits, "claude_subscription")
    assert row.state == "read"
    assert "seven_day_utilization" not in row.values
    assert "seven_day_utilization" in row.unknown_fields


def test_limits_cloud_credit_row_carries_remaining_source_and_the_contract_expiry() -> (
    None
):
    limits = with_accounts(account())
    (row,) = rows_of(limits, "cloud_credit")
    assert row.name == "acct-a"
    assert row.state == "read"
    assert row.values["remaining"] == pytest.approx(25.5)
    assert row.values["expires_on"] == config().limits.cloud_credit_expiry
    assert row.source == "claude.ai/settings/usage"
    assert row.read_at == stamp(2)


def test_limits_api_credit_row_projects_the_unspent_budget_at_month_end() -> None:
    # 30.5 of the 31 days of October have passed at NOW: 61 USD so far projects to 62 USD for the month.
    key = ModelLimitApiKey(
        name="anthropic-key-1",
        month_spend_usd=61.0,
        read_at=stamp(1),
        source="api-credit-meter",
    )
    limits = limits_of(api_keys=(key,))
    (row,) = rows_of(limits, "api_credit")
    assert row.state == "read"
    assert row.source == "api-credit-meter"
    assert row.values["month_spend_usd"] == pytest.approx(61.0)
    assert row.values["budget_usd"] == pytest.approx(config().limits.api_budget_usd)
    assert row.values["remaining_usd"] == pytest.approx(139.0)
    assert row.values["projected_month_spend_usd"] == pytest.approx(62.0)
    assert row.values["projected_unspent_usd"] == pytest.approx(138.0)


def test_limits_api_credit_a_key_budget_from_the_overlay_replaces_the_contract_value() -> (
    None
):
    key = ModelLimitApiKey(
        name="k",
        month_spend_usd=10.0,
        read_at=stamp(1),
        source="api-credit-meter",
        budget_usd=50.0,
    )
    (row,) = rows_of(limits_of(api_keys=(key,)), "api_credit")
    assert row.values["budget_usd"] == pytest.approx(50.0)
    assert row.values["remaining_usd"] == pytest.approx(40.0)


def test_limits_engines_codex_glm_and_each_lab_rung_have_a_row() -> None:
    limits = limits_of(
        codex=ModelLimitEngine(
            used_pct=0.3,
            window="5h",
            resets_at="2026-10-31T16:00:00Z",
            read_at=stamp(1),
            source="codex usage",
        ),
        glm=ModelLimitEngine(
            used_pct=0.01,
            window="weekly",
            resets_at="2026-11-03T00:00:00Z",
            read_at=stamp(2),
            source="glm coding plan",
        ),
        lab_rungs=(
            ModelLimitLabRung(
                name="qwen-27b-h201",
                available=True,
                last_smoke_at=stamp(20),
                last_smoke_ok=True,
                read_at=stamp(1),
                source="rung canary",
            ),
            ModelLimitLabRung(
                name="qwen-27b-h202",
                available=False,
                last_smoke_at=stamp(30),
                last_smoke_ok=False,
                read_at=stamp(1),
                source="rung canary",
            ),
        ),
    )
    (codex,) = rows_of(limits, "codex")
    assert codex.state == "read"
    assert codex.values["used_pct"] == pytest.approx(0.3)
    assert codex.values["window"] == "5h"
    assert codex.values["resets_at"] == "2026-10-31T16:00:00Z"
    (glm,) = rows_of(limits, "glm")
    assert glm.values["used_pct"] == pytest.approx(0.01)
    rungs = {r.name: r for r in rows_of(limits, "lab_model_rung")}
    assert set(rungs) == {"qwen-27b-h201", "qwen-27b-h202"}
    assert rungs["qwen-27b-h201"].values["available"] is True
    assert rungs["qwen-27b-h201"].values["last_smoke_at"] == stamp(20)
    assert rungs["qwen-27b-h202"].values["available"] is False
    assert rungs["qwen-27b-h202"].values["last_smoke_ok"] is False


def test_limits_engines_codex_and_glm_rows_exist_even_when_nothing_was_read() -> None:
    limits = limits_of()
    (codex,) = rows_of(limits, "codex")
    (glm,) = rows_of(limits, "glm")
    assert codex.state == glm.state == "UNKNOWN"
    assert "lab_rungs" in limits.unread_sources


def test_limits_token_pool_revoked_is_counted_as_revoked_and_never_as_capacity() -> (
    None
):
    pool = ModelLimitTokenPool(
        host="h200",
        read_at=stamp(1),
        source="claude-token-pool.json",
        tokens=(
            ModelLimitToken(label="a"),
            ModelLimitToken(label="b", limited_until="2026-10-31T13:00:00Z"),
            # revoked with a future limit: still revoked, not limited, not usable
            ModelLimitToken(
                label="c", limited_until="2026-10-31T13:00:00Z", revoked=True
            ),
            ModelLimitToken(label="d", revoked=True),
            # a limit that has already passed is capacity again
            ModelLimitToken(label="e", limited_until="2026-10-31T10:00:00Z"),
        ),
    )
    (row,) = rows_of(limits_of(token_pools=(pool,)), "host_account_pool")
    assert row.name == "h200"
    assert row.state == "read"
    assert row.values["revoked"] == 2
    assert row.values["limited"] == 1
    assert row.values["usable"] == 2
    assert row.values["total"] == 5
    assert row.values["limited_until"] == "2026-10-31T13:00:00Z"
    assert row.values["usable"] + row.values["limited"] + row.values["revoked"] == 5


def test_limits_token_pool_revoked_every_token_revoked_leaves_no_capacity() -> None:
    pool = ModelLimitTokenPool(
        host="h201",
        read_at=stamp(1),
        source="claude-token-pool.json",
        tokens=(
            ModelLimitToken(label="a", revoked=True),
            ModelLimitToken(label="b", revoked=True),
        ),
    )
    (row,) = rows_of(limits_of(token_pools=(pool,)), "host_account_pool")
    assert row.values["usable"] == 0
    assert row.values["revoked"] == 2


def test_limits_unknown_never_zero_a_row_never_read_says_unknown_with_no_number() -> (
    None
):
    limits = limits_of(
        api_keys=(
            ModelLimitApiKey(
                name="k", month_spend_usd=None, read_at="", source="api-credit-meter"
            ),
        ),
        token_pools=(
            ModelLimitTokenPool(
                host="h200", read_at="", source="claude-token-pool.json", tokens=()
            ),
        ),
    )
    for kind in ("api_credit", "host_account_pool"):
        (row,) = rows_of(limits, kind)
        assert row.state == "UNKNOWN"
        assert row.values == {}
        assert row.read_at == ""
        assert row.unknown_reason
        assert numbers_in(json.loads(row.model_dump_json())["values"]) == []


def test_limits_unknown_never_zero_a_row_older_than_one_publish_cycle_says_unknown() -> (
    None
):
    cycle = config().limits.publish_cycle_minutes
    old = stamp(cycle + 1)
    fresh = stamp(cycle - 1)
    stale_key = ModelLimitApiKey(
        name="stale", month_spend_usd=0.0, read_at=old, source="api-credit-meter"
    )
    fresh_key = ModelLimitApiKey(
        name="fresh", month_spend_usd=0.0, read_at=fresh, source="api-credit-meter"
    )
    rows = {
        r.name: r
        for r in rows_of(limits_of(api_keys=(stale_key, fresh_key)), "api_credit")
    }
    assert rows["stale"].state == "UNKNOWN"
    assert rows["stale"].values == {}
    assert rows["stale"].read_at == old
    # a fresh reading of a real zero spend is a zero, and is drawn as one
    assert rows["fresh"].state == "read"
    assert rows["fresh"].values["month_spend_usd"] == 0.0


def test_limits_unknown_never_zero_every_row_names_its_source_and_a_stale_account_is_unknown() -> (
    None
):
    cycle = config().limits.publish_cycle_minutes
    limits = with_accounts(
        account(rate_read_at=stamp(cycle + 5), credit_read_at=""),
    )
    for row in limits.rows:
        assert row.source
        if row.kind in ("claude_subscription", "cloud_credit"):
            assert row.state == "UNKNOWN"
            assert row.values == {}


def test_limits_unknown_never_zero_an_unreadable_source_is_listed_not_drawn_as_zero_rows() -> (
    None
):
    result = HandlerFactoryFloor().handle(request())
    limits = result.limits
    assert limits.rows[0].kind in ("codex", "glm")
    assert {"cloud_accounts", "api_keys", "token_pools", "lab_rungs"} <= set(
        limits.unread_sources
    )
    assert [r for r in limits.rows if r.state == "read"] == []


def test_limits_record_json_carries_the_limits_section() -> None:
    result = HandlerFactoryFloor().handle(request())
    record = json.loads(result.model_dump_json())
    assert (
        record["limits"]["publish_cycle_minutes"]
        == config().limits.publish_cycle_minutes
    )
    assert isinstance(record["limits"]["rows"], list)
