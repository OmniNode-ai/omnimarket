# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Limits section of the factory floor record (OMN-22010): a row per account and engine, UNKNOWN never zero."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount
from omnimarket.models.model_floor_limits_input import (
    ModelFloorApiKeySpend,
    ModelFloorApiMeter,
    ModelFloorEngineReadings,
    ModelFloorHostTokenPool,
    ModelFloorIdleCarry,
    ModelFloorIdleHostReading,
    ModelFloorIdleReading,
    ModelFloorLabModelRung,
    ModelFloorLimitsConfig,
    ModelFloorLimitsInput,
    ModelFloorPoolToken,
    ModelFloorUsageWindow,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_limits import (
    build_limits,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_floor_limits import (
    ModelFloorLimitRow,
    ModelFloorLimits,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.limit_sources import (
    load_api_meter,
    load_engines,
    load_idle_carry,
    load_token_pool,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 3, 16, 0, 0, tzinfo=UTC)
CONFIG = ModelFloorLimitsConfig(
    publish_cycle_seconds=300, api_budget_usd=200, projection_min_elapsed_hours=24
)


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def limits(**fields: Any) -> ModelFloorLimits:
    return build_limits(ModelFloorLimitsInput(now=NOW, config=CONFIG, **fields))


def rows(result: ModelFloorLimits, kind: str) -> list[ModelFloorLimitRow]:
    return [r for r in result.rows if r.kind == kind]


def values(row: ModelFloorLimitRow) -> dict[str, object]:
    return {v.name: v.value for v in row.values}


def account(name: str = "h200", **fields: Any) -> ModelFloorCloudAccount:
    base: dict[str, Any] = {
        "name": name,
        "login": "ok",
        "rate_status": "allowed",
        "rate_type": "seven_day",
        "rate_utilization": 0.62,
        "rate_resets_at": "2026-11-04T22:00:00Z",
        "rate_read_at": stamp(1),
        "credit_remaining": 244.0,
        "credit_read_at": stamp(2),
        "credit_expires": "2026-11-05",
        "credit_source": "operator reading",
    }
    return ModelFloorCloudAccount(**{**base, **fields})


def test_claude_subscription_row_per_account_with_both_windows_and_the_hold() -> None:
    result = limits(
        cloud_accounts=(
            account("h200", limited_until="2026-11-04T22:00:00Z"),
            account(
                "h201",
                rate_type="five_hour",
                rate_utilization=0.4,
                rate_resets_at="2026-11-03T19:00:00Z",
            ),
        ),
        cloud_accounts_source="cloud-accounts-v2.json",
    )
    h200, h201 = rows(result, "claude_subscription")
    assert (h200.name, h201.name) == ("h200", "h201")
    assert h200.status == h201.status == "READ"
    assert values(h200) == {
        "five_hour_utilization": None,
        "five_hour_resets_at": None,
        "seven_day_utilization": 0.62,
        "seven_day_resets_at": "2026-11-04T22:00:00Z",
        "rate_status": "allowed",
        "limited_until": "2026-11-04T22:00:00Z",
    }
    assert values(h201)["five_hour_utilization"] == 0.4
    assert values(h201)["five_hour_resets_at"] == "2026-11-03T19:00:00Z"
    assert values(h201)["seven_day_utilization"] is None
    # A hold already over is no hold.
    assert values(h201)["limited_until"] == ""
    assert h200.source == "cloud-accounts-v2.json#h200.rate"


def test_cloud_credit_row_per_account_with_balance_and_expiry() -> None:
    result = limits(
        cloud_accounts=(account("h200"), account("h201", credit_remaining=None)),
        cloud_accounts_source="cloud-accounts-v2.json",
    )
    h200, h201 = rows(result, "cloud_credit")
    assert values(h200) == {
        "credit_remaining_usd": 244.0,
        "credit_expires": "2026-11-05",
        "credit_source": "operator reading",
    }
    assert h201.status == "UNKNOWN"
    assert h201.values == ()
    assert h201.cause == "no credit reading recorded"


def test_api_credit_row_per_key_with_spend_remaining_and_projected_unspent() -> None:
    # Nov 3 16:00 UTC is 2 days 16 h into a 30-day month: 64 h of 720 h.
    meter = ModelFloorApiMeter(
        read_at=stamp(1),
        keys=(
            ModelFloorApiKeySpend(name="key-a", month="2026-11", month_spend_usd=32.0),
        ),
    )
    result = limits(
        api_meter=meter,
        api_meter_source="api-credit-meter.json",
        api_key_names=("key-a", "key-b"),
    )
    a, b = rows(result, "api_credit")
    assert values(a) == {
        "month_spend_usd": 32.0,
        "budget_usd": 200,
        "remaining_usd": 168.0,
        "projected_month_end_spend_usd": pytest.approx(360.0),
        "projected_unspent_usd": pytest.approx(-160.0),
    }
    assert a.source == "api-credit-meter.json"
    # A key the deployment expects and the meter does not list is UNKNOWN, not zero spend.
    assert (b.status, b.values) == ("UNKNOWN", ())
    assert "does not list" in b.cause


def test_api_credit_reading_of_another_month_is_unknown_and_early_month_draws_no_projection() -> (
    None
):
    meter = ModelFloorApiMeter(
        read_at=stamp(1),
        keys=(ModelFloorApiKeySpend(name="old", month="2026-10", month_spend_usd=5.0),),
    )
    (old,) = rows(limits(api_meter=meter), "api_credit")
    assert old.status == "UNKNOWN"
    assert "2026-10" in old.cause
    early = build_limits(
        ModelFloorLimitsInput(
            now=datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
            config=CONFIG,
            api_meter=ModelFloorApiMeter(
                read_at="2026-11-01T02:59:00Z",
                keys=(
                    ModelFloorApiKeySpend(
                        name="k", month="2026-11", month_spend_usd=1.0
                    ),
                ),
            ),
        )
    )
    (row,) = rows(early, "api_credit")
    assert values(row)["projected_unspent_usd"] is None
    assert values(row)["remaining_usd"] == 199.0


def test_engines_codex_glm_and_lab_rungs_each_have_a_row() -> None:
    engines = ModelFloorEngineReadings(
        codex=ModelFloorUsageWindow(
            read_at=stamp(1),
            window="5h",
            used_pct=41.0,
            resets_at="2026-11-03T18:00:00Z",
        ),
        glm=ModelFloorUsageWindow(read_at=stamp(1), window="weekly", used_pct=1.0),
        lab_models=(
            ModelFloorLabModelRung(
                name="qwen3-27b",
                read_at=stamp(1),
                available=True,
                last_smoke_at=stamp(10),
            ),
            ModelFloorLabModelRung(name="r1-14b", read_at=stamp(1), available=False),
        ),
    )
    result = limits(engines=engines, engines_source="engine-readings.json")
    (codex,) = rows(result, "codex")
    (glm,) = rows(result, "glm")
    qwen, r1 = rows(result, "lab_model")
    assert values(codex) == {
        "window": "5h",
        "used_pct": 41.0,
        "resets_at": "2026-11-03T18:00:00Z",
    }
    assert values(glm)["used_pct"] == 1.0
    assert values(glm)["resets_at"] is None
    assert values(qwen) == {"available": True, "last_smoke_at": stamp(10)}
    # Not available is a reading; a smoke that never ran is None, not a time.
    assert values(r1) == {"available": False, "last_smoke_at": None}


def test_engines_unread_source_gives_unknown_rows_for_codex_glm_and_lab_rungs() -> None:
    result = limits(
        engines=None, causes={"engines": "engine-readings.json does not exist"}
    )
    assert {r.kind for r in result.rows if r.kind in {"codex", "glm", "lab_model"}} == {
        "codex",
        "glm",
        "lab_model",
    }
    for r in result.rows:
        if r.kind in {"codex", "glm", "lab_model"}:
            assert (r.status, r.values, r.read_at) == ("UNKNOWN", (), "")
            assert r.cause == "engine-readings.json does not exist"


def test_token_pool_revoked_is_counted_as_revoked_and_never_as_capacity() -> None:
    pool = ModelFloorHostTokenPool(
        host="h200",
        read_at=stamp(1),
        tokens=(
            ModelFloorPoolToken(account="a", state="usable"),
            ModelFloorPoolToken(account="b", state="revoked"),
            # A revoked token that still names a past or future hold stays revoked.
            ModelFloorPoolToken(
                account="c", state="revoked", limited_until="2020-01-01T00:00:00Z"
            ),
            ModelFloorPoolToken(
                account="d", state="limited", limited_until="2026-11-04T01:00:00Z"
            ),
            # A hold already over is capacity again.
            ModelFloorPoolToken(
                account="e", state="limited", limited_until="2026-11-03T10:00:00Z"
            ),
            # Limited with no readable end is not capacity.
            ModelFloorPoolToken(account="f", state="limited"),
        ),
    )
    (row,) = rows(
        limits(token_pool=(pool,), token_pool_source="token-pool.json"), "account_pool"
    )
    assert values(row) == {
        "usable": 2,
        "limited": 2,
        "limited_until": "2026-11-04T01:00:00Z",
        "revoked": 2,
    }
    all_revoked = ModelFloorHostTokenPool(
        host="h201",
        read_at=stamp(1),
        tokens=(ModelFloorPoolToken(account="x", state="revoked"),),
    )
    (only,) = rows(limits(token_pool=(all_revoked,)), "account_pool")
    assert values(only)["usable"] == 0
    assert values(only)["revoked"] == 1


def test_unknown_never_zero_stale_or_never_read_rows_carry_no_values() -> None:
    result = limits(
        cloud_accounts=(
            account("fresh"),
            account("stale", rate_read_at=stamp(6), credit_read_at=""),
            account("never", rate_read_at=""),
        ),
        cloud_accounts_source="cloud-accounts-v2.json",
        token_pool=(
            ModelFloorHostTokenPool(
                host="h1",
                read_at=stamp(20),
                tokens=(ModelFloorPoolToken(account="a", state="usable"),),
            ),
            ModelFloorHostTokenPool(host="h2", read_at=stamp(1), tokens=()),
        ),
    )
    sub = {r.name: r for r in rows(result, "claude_subscription")}
    assert sub["fresh"].status == "READ"
    assert sub["stale"].status == "UNKNOWN"
    assert "older than one publish cycle (300s)" in sub["stale"].cause
    # The last read time stays on the row so the reader sees how old it is.
    assert sub["stale"].read_at == stamp(6)
    assert sub["never"].cause == "no rate headers recorded for the account"
    credit = {r.name: r for r in rows(result, "cloud_credit")}
    assert credit["stale"].cause == "never read"
    pools = {r.name: r for r in rows(result, "account_pool")}
    assert pools["h1"].status == "UNKNOWN"
    assert pools["h1"].values == ()
    assert pools["h2"].status == "UNKNOWN"
    assert "no token" in pools["h2"].cause
    for r in result.rows:
        assert (r.status == "UNKNOWN") == (r.values == ()), r.row_id
        assert r.source or r.status == "UNKNOWN"


def test_unknown_never_zero_unread_sources_draw_one_unknown_row_each_with_the_cause() -> (
    None
):
    result = limits(causes={"token_pool": "token-pool.json does not exist"})
    assert {r.kind for r in result.rows} == {
        "claude_subscription",
        "cloud_credit",
        "api_credit",
        "codex",
        "glm",
        "lab_model",
        "account_pool",
    }
    assert all(r.status == "UNKNOWN" and r.values == () for r in result.rows)
    (pool,) = rows(result, "account_pool")
    assert pool.cause == "token-pool.json does not exist"
    assert result.idle.state == "unread"
    assert result.idle.idle_slot_hours is None


def idle_input(carry: ModelFloorIdleCarry | None, **idle: Any) -> ModelFloorLimitsInput:
    reading = ModelFloorIdleReading(
        day="2026-11-03",
        hosts=(
            ModelFloorIdleHostReading(host="h200", state="read", slots=8, idle=3),
            ModelFloorIdleHostReading(host="h201", state="unread"),
        ),
        idle_total=3,
        idle_unread=1,
        **idle,
    )
    return ModelFloorLimitsInput(now=NOW, config=CONFIG, idle=reading, idle_carry=carry)


def test_idle_slot_hours_accrue_from_the_previous_record_within_two_publish_cycles() -> (
    None
):
    first = build_limits(idle_input(None)).idle
    assert first.idle_slot_hours is None
    assert "first reading" in first.basis
    carry = ModelFloorIdleCarry(
        generated_at=NOW - timedelta(minutes=5),
        day="2026-11-03",
        idle_total=6,
        idle_slot_hours=10.0,
    )
    idle = build_limits(idle_input(carry)).idle
    # 6 idle slots for 5 minutes = 0.5 slot-hours on top of 10.
    assert idle.idle_slot_hours == 10.5
    assert idle.unread_minutes == 0
    assert [(h.host, h.idle) for h in idle.hosts] == [("h200", 3), ("h201", None)]
    gap = ModelFloorIdleCarry(
        generated_at=NOW - timedelta(minutes=40),
        day="2026-11-03",
        idle_total=6,
        idle_slot_hours=10.0,
    )
    late = build_limits(idle_input(gap)).idle
    # Only two cycles (10 min) are counted; the other 30 minutes are listed as unread.
    assert late.idle_slot_hours == 11.0
    assert late.unread_minutes == 30.0
    other_day = ModelFloorIdleCarry(
        generated_at=NOW - timedelta(minutes=5),
        day="2026-11-02",
        idle_total=6,
        idle_slot_hours=99.0,
    )
    assert build_limits(idle_input(other_day)).idle.idle_slot_hours is None


def test_sources_read_their_files_and_name_why_a_file_is_unread(tmp_path: Path) -> None:
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(
        json.dumps(
            {
                "hosts": {
                    "h200": {
                        "read_at": stamp(1),
                        "tokens": [{"account": "a", "state": "revoked"}],
                    }
                }
            }
        )
    )
    pool, cause = load_token_pool(pool_file)
    assert cause == ""
    assert pool is not None
    assert pool[0].tokens[0].state == "revoked"
    missing, why = load_token_pool(tmp_path / "absent.json")
    assert missing is None
    assert why == "absent.json does not exist"
    pool_file.write_text(
        json.dumps({"hosts": {"h": {"tokens": [{"account": "a", "state": "bogus"}]}}})
    )
    bad, why = load_token_pool(pool_file)
    assert bad is None
    assert "malformed" in why
    meter_file = tmp_path / "meter.json"
    meter_file.write_text(
        json.dumps(
            {
                "read_at": stamp(1),
                "keys": {"k": {"month": "2026-11", "month_spend_usd": 3}},
            }
        )
    )
    meter, _ = load_api_meter(meter_file)
    assert meter is not None
    assert meter.keys[0].month_spend_usd == 3.0
    engines_file = tmp_path / "engines.json"
    engines_file.write_text(
        json.dumps(
            {
                "codex": {"read_at": stamp(1), "used_pct": 7},
                "lab_models": {"m": {"read_at": stamp(1), "available": True}},
            }
        )
    )
    engines, _ = load_engines(engines_file)
    assert engines is not None
    assert engines.glm is None
    assert engines.codex is not None
    assert engines.codex.used_pct == 7.0
    assert engines.lab_models[0].available is True


def test_idle_carry_reads_the_previous_record_and_ignores_one_without_limits(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "factory-floor.json"
    assert load_idle_carry(previous) is None
    previous.write_text(json.dumps({"floor": {}}))
    assert load_idle_carry(previous) is None
    result = build_limits(idle_input(None))
    previous.write_text(
        json.dumps({"floor": {"limits": result.model_dump(mode="json")}})
    )
    carry = load_idle_carry(previous)
    assert carry is not None
    assert (carry.day, carry.idle_total) == ("2026-11-03", 3)
