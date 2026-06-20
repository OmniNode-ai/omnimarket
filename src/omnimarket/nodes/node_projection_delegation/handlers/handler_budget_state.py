# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event-sourced per-tenant ceiling budget-state reducer (OMN-13235).

Materializes ``delegation_budget_state`` (migration 0019) from delegation source
events. A ``budgeted`` ceiling tier (EnumTierCostType.BUDGETED, OMN-13234) carries
a ``monthly_cap_usd``: tokens served while headroom remains cost 0 cash and draw
the cap down; tokens past the cap bill overage. This reducer accumulates each
delegation's measured drawdown (``budget_headroom_consumed_usd``, OMN-13355 / 0018)
plus any cash overage into the tenant's monthly period row, and derives the
remaining headroom. Idempotent per source event via ``last_correlation_id`` so a
replayed event never double-counts.

The reducer is deterministic and depends only on the source event + the routing
registry cap; the budget store mutation is the projection UPSERT itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.pricing import resolve_tier_cost
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "delegation_budget_state"
CONFLICT_KEY = "tenant_id,cost_tier_name,budget_period"
DEFAULT_TENANT = "default"


class ModelDelegationBudgetStateEvent(BaseModel):
    """Source-event fields needed to update a tenant's ceiling budget state.

    Parsed from a delegation source event (task-delegated / delegation-completed).
    ``extra="ignore"`` so the rich payload parses cleanly. The reducer only acts
    on events whose serving tier resolves to a ``budgeted`` cost model with a cap.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., min_length=1)
    cost_tier_name: str = Field(default="")
    cost_measurement_source: str = Field(default="")
    budget_headroom_consumed_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    tenant_id: str | None = Field(default=None)
    timestamp: str | None = Field(default=None)

    def resolved_tenant(self) -> str:
        if self.tenant_id and self.tenant_id.strip():
            return self.tenant_id
        return DEFAULT_TENANT

    def resolved_event_time(self) -> datetime:
        if self.timestamp:
            try:
                parsed = datetime.fromisoformat(self.timestamp)
            except ValueError:
                return datetime.now(tz=UTC)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return datetime.now(tz=UTC)

    def budget_period(self) -> str:
        """The UTC month (YYYY-MM) the cap applies to."""
        return self.resolved_event_time().strftime("%Y-%m")


class ModelBudgetStateResult(BaseModel):
    """Outcome of a budget-state materialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)
    skipped_reason: str = Field(default="")


def materialize_budget_state(
    event: ModelDelegationBudgetStateEvent,
    db: DatabaseAdapter,
) -> ModelBudgetStateResult:
    """Accumulate one delegation's drawdown into the tenant's ceiling budget row.

    Only ``budgeted`` tiers with a resolvable monthly cap produce a row — for
    free_local / metered tiers there is no ceiling budget to track, so the reducer
    returns a no-row result (truthful empty). Idempotent: a source event whose
    correlation_id already wrote the current period row is skipped.
    """
    cost = resolve_tier_cost(event.cost_tier_name)
    if cost is None or cost.monthly_cap_usd is None:
        return ModelBudgetStateResult(skipped_reason="tier_not_budgeted")

    tenant_id = event.resolved_tenant()
    period = event.budget_period()
    cap = Decimal(str(cost.monthly_cap_usd))
    drawdown = event.budget_headroom_consumed_usd
    overage = event.cost_usd
    now_iso = datetime.now(tz=UTC).isoformat()
    event_iso = event.resolved_event_time().isoformat()

    existing_rows = db.query(
        TABLE,
        {
            "tenant_id": tenant_id,
            "cost_tier_name": event.cost_tier_name,
            "budget_period": period,
        },
    )
    if existing_rows:
        existing = existing_rows[0]
        # Idempotent replay guard: the same source event already applied.
        if str(existing.get("last_correlation_id") or "") == event.correlation_id:
            return ModelBudgetStateResult(rows_upserted=0, skipped_reason="replayed")
        consumed = _as_decimal(existing.get("consumed_usd")) + drawdown
        overage_total = _as_decimal(existing.get("overage_usd")) + overage
        count = _as_int(existing.get("delegation_count")) + 1
        first_event_at = str(existing.get("first_event_at") or event_iso)
    else:
        consumed = drawdown
        overage_total = overage
        count = 1
        first_event_at = event_iso

    headroom_remaining = cap - consumed
    if headroom_remaining < Decimal("0"):
        headroom_remaining = Decimal("0")

    row: dict[str, object] = {
        "tenant_id": tenant_id,
        "cost_tier_name": event.cost_tier_name,
        "budget_period": period,
        "monthly_cap_usd": cap,
        "consumed_usd": consumed,
        "overage_usd": overage_total,
        "headroom_remaining_usd": headroom_remaining,
        "delegation_count": count,
        "last_correlation_id": event.correlation_id,
        "first_event_at": first_event_at,
        "last_event_at": event_iso,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    ok = db.upsert(TABLE, CONFLICT_KEY, row)
    return ModelBudgetStateResult(rows_upserted=1 if ok else 0)


def _as_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ValueError, ArithmeticError):
            return Decimal("0")
    return Decimal("0")


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


__all__: list[str] = [
    "CONFLICT_KEY",
    "DEFAULT_TENANT",
    "TABLE",
    "ModelBudgetStateResult",
    "ModelDelegationBudgetStateEvent",
    "materialize_budget_state",
]
