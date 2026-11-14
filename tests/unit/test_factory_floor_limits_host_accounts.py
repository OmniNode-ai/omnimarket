# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Limits panel's host account pool row counts each host's accounts by usage verdict (OMN-23609).

Each host shows its usable and limited accounts, judged by the pool usage verdict (ok, drain, limited, unknown). An
account with no recent reading is unknown and is never counted usable; with no thresholds nothing is judged and the
counts read UNKNOWN, never zero.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelFloorLimitsFacts,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorLimitRow,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.floor_account_verdicts import (
    account_verdicts_from_floor,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolUsagePolicy,
    ModelPoolUsageWindowPolicy,
)
from tests.nodes.node_factory_floor_compute.support import NOW, request

POLICY = ModelPoolUsagePolicy(
    five_hour=ModelPoolUsageWindowPolicy(drain_at=0.9, limited_at=1.0),
    weekly=ModelPoolUsageWindowPolicy(drain_at=0.85, limited_at=1.0),
    max_reading_age_seconds=900,
)
COUNTS = (
    "accounts_usable",
    "accounts_draining",
    "accounts_limited",
    "accounts_unknown",
)


def stamp(minutes_ago: float) -> str:
    return f"{NOW - timedelta(minutes=minutes_ago):%Y-%m-%dT%H:%M:%SZ}"


def later(hours: float) -> str:
    return f"{NOW + timedelta(hours=hours):%Y-%m-%dT%H:%M:%SZ}"


def account(name: str, **over: Any) -> ModelFloorCloudAccount:
    base: dict[str, Any] = {
        "name": name,
        "rate_read_at": stamp(1),
        "five_hour_utilization": 0.2,
        "five_hour_resets_at": later(3),
        "seven_day_utilization": 0.1,
        "seven_day_resets_at": later(48),
    }
    base.update(over)
    return ModelFloorCloudAccount(**base)


def pool(host: str, *tokens: ModelLimitToken) -> ModelLimitTokenPool:
    return ModelLimitTokenPool(
        host=host, read_at=stamp(1), source="claude-token-pool.json", tokens=tokens
    )


def host_rows(
    accounts: tuple[ModelFloorCloudAccount, ...],
    pools: tuple[ModelLimitTokenPool, ...],
    policy: ModelPoolUsagePolicy | None = POLICY,
) -> dict[str, ModelFloorLimitRow]:
    """Judge the accounts exactly as the effect node does, then draw the panel."""
    verdicts = account_verdicts_from_floor(accounts, pools, NOW, policy)
    result = HandlerFactoryFloor().handle(
        request(
            cloud_accounts=accounts,
            limits=ModelFloorLimitsFacts(token_pools=pools, account_verdicts=verdicts),
        )
    )
    return {r.name: r for r in result.limits.rows if r.kind == "host_account_pool"}


def test_host_row_lists_usable_and_limited_accounts_per_host() -> None:
    accounts = (
        account("a"),
        account("b"),
        account("c", five_hour_utilization=1.0),
    )
    rows = host_rows(
        accounts,
        (
            pool("h200", ModelLimitToken(label="a"), ModelLimitToken(label="b")),
            pool("h201", ModelLimitToken(label="a"), ModelLimitToken(label="c")),
        ),
    )
    assert set(rows) == {"h200", "h201"}
    assert [rows["h200"].values[k] for k in COUNTS] == [2, 0, 0, 0]
    assert [rows["h201"].values[k] for k in COUNTS] == [1, 0, 1, 0]


def test_account_with_a_stale_reading_is_unknown_and_never_counted_usable() -> None:
    accounts = (account("a"), account("b", rate_read_at=stamp(60)))
    (row,) = host_rows(
        accounts,
        (pool("h200", ModelLimitToken(label="a"), ModelLimitToken(label="b")),),
    ).values()
    assert row.values["accounts_usable"] == 1
    assert row.values["accounts_unknown"] == 1
    assert row.values["accounts_limited"] == 0


def test_account_never_read_or_absent_from_the_accounts_is_unknown() -> None:
    accounts = (account("a", rate_read_at="", five_hour_utilization=None),)
    (row,) = host_rows(
        accounts,
        (pool("h200", ModelLimitToken(label="a"), ModelLimitToken(label="ghost")),),
    ).values()
    assert row.values["accounts_usable"] == 0
    assert row.values["accounts_unknown"] == 2


def test_draining_account_is_usable_and_also_counted_draining() -> None:
    accounts = (account("a", five_hour_utilization=0.95),)
    (row,) = host_rows(accounts, (pool("h200", ModelLimitToken(label="a")),)).values()
    assert [row.values[k] for k in COUNTS] == [1, 1, 0, 0]


def test_host_limit_mark_and_revoked_token_are_never_usable() -> None:
    accounts = (account("a"), account("b"), account("c"))
    (row,) = host_rows(
        accounts,
        (
            pool(
                "h200",
                ModelLimitToken(label="a", limited_until=later(2)),
                ModelLimitToken(label="b", revoked=True),
                ModelLimitToken(label="c"),
            ),
        ),
    ).values()
    assert [row.values[k] for k in COUNTS] == [1, 0, 1, 0]
    assert row.values["revoked"] == 1


def test_without_thresholds_the_account_counts_are_unknown_not_zero() -> None:
    (row,) = host_rows(
        (account("a"),), (pool("h200", ModelLimitToken(label="a")),), policy=None
    ).values()
    assert row.state == "read"
    for key in COUNTS:
        assert key not in row.values
    assert set(COUNTS) <= set(row.unknown_fields)


def test_existing_pool_values_keep_their_shape() -> None:
    (row,) = host_rows(
        (account("a"),), (pool("h200", ModelLimitToken(label="a")),)
    ).values()
    assert {"total", "revoked", "limited", "usable"} <= set(row.values)
