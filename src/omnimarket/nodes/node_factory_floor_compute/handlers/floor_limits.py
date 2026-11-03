# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the Limits section of the factory floor record (OMN-22010).

Pure: no clock and no I/O, the readings arrive in ``ModelFloorLimitsInput``. Every row carries the source it was read
from and its read time. A row whose read time is missing, unreadable or older than one publish cycle is UNKNOWN and
carries no value, so a source nobody read can never draw as zero.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount
from omnimarket.models.model_floor_limits_input import (
    ModelFloorEngineReadings,
    ModelFloorHostTokenPool,
    ModelFloorLimitsInput,
    ModelFloorUsageWindow,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_floor_limits import (
    LimitKind,
    ModelFloorIdleHostOut,
    ModelFloorLimitRow,
    ModelFloorLimits,
    ModelFloorLimitsIdle,
    ModelFloorLimitValue,
)

SUBSCRIPTION_WINDOWS = ("five_hour", "seven_day")
# Idle slots accrue over at most this many publish cycles after a record; the rest of the gap is counted as unread.
ACCRUAL_CYCLES = 2
IDLE_BASIS = (
    "each record's idle slots times the time to the next record, within two publish cycles; "
    "time beyond that is not counted and is listed as unread"
)


def _moment(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _stale(read_at: str, now: datetime, cycle: int) -> str:
    """Why a reading cannot be shown, or the empty string when it is fresh."""
    if not read_at:
        return "never read"
    moment = _moment(read_at)
    if moment is None:
        return f"read time unreadable ({read_at})"
    age = (now - moment).total_seconds()
    if age > cycle:
        return f"read {_age(age)} ago, older than one publish cycle ({cycle}s)"
    return ""


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _val(
    name: str, value: float | int | str | bool | None, unit: str = ""
) -> ModelFloorLimitValue:
    return ModelFloorLimitValue(name=name, value=value, unit=unit)


def _row(
    kind: LimitKind,
    key: str,
    name: str,
    source: str,
    read_at: str,
    now: datetime,
    cycle: int,
    values: tuple[ModelFloorLimitValue, ...],
    unread: str = "",
) -> ModelFloorLimitRow:
    """A READ row, or an UNKNOWN one (no values) when ``unread`` names a cause or the read time is not fresh."""
    cause = unread or _stale(read_at, now, cycle)
    return ModelFloorLimitRow(
        row_id=f"{kind}:{key}",
        kind=kind,
        name=name,
        status="UNKNOWN" if cause else "READ",
        source=source,
        read_at=read_at,
        cause=cause,
        values=() if cause else values,
    )


def _unread_row(
    kind: LimitKind, key: str, name: str, source: str, cause: str
) -> ModelFloorLimitRow:
    return ModelFloorLimitRow(
        row_id=f"{kind}:{key}",
        kind=kind,
        name=name,
        status="UNKNOWN",
        source=source,
        read_at="",
        cause=cause,
        values=(),
    )


def _later(text: str, now: datetime) -> bool:
    moment = _moment(text)
    return moment is not None and moment > now


def _subscription_rows(
    inp: ModelFloorLimitsInput,
) -> list[ModelFloorLimitRow]:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    if inp.cloud_accounts is None:
        cause = (
            inp.causes.get("cloud_accounts") or "the cloud accounts file was not read"
        )
        return [
            _unread_row(
                "claude_subscription",
                "all",
                "Claude subscription accounts",
                inp.cloud_accounts_source,
                cause,
            )
        ]
    return [
        _subscription_row(a, inp.cloud_accounts_source, now, cycle)
        for a in inp.cloud_accounts
    ]


def _subscription_row(
    account: ModelFloorCloudAccount, source: str, now: datetime, cycle: int
) -> ModelFloorLimitRow:
    windows: dict[str, tuple[float | None, str | None]] = dict.fromkeys(
        SUBSCRIPTION_WINDOWS, (None, None)
    )
    if account.rate_type in windows:
        windows[account.rate_type] = (
            account.rate_utilization,
            account.rate_resets_at or None,
        )
    values: list[ModelFloorLimitValue] = []
    for window in SUBSCRIPTION_WINDOWS:
        utilization, resets_at = windows[window]
        values.append(_val(f"{window}_utilization", utilization, "ratio"))
        values.append(_val(f"{window}_resets_at", resets_at))
    values.append(_val("rate_status", account.rate_status or None))
    values.append(
        _val(
            "limited_until",
            account.limited_until if _later(account.limited_until, now) else "",
        )
    )
    return _row(
        "claude_subscription",
        account.name,
        account.name,
        f"{source}#{account.name}.rate",
        account.rate_read_at,
        now,
        cycle,
        tuple(values),
        unread=""
        if account.rate_read_at
        else "no rate headers recorded for the account",
    )


def _credit_rows(inp: ModelFloorLimitsInput) -> list[ModelFloorLimitRow]:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    if inp.cloud_accounts is None:
        cause = (
            inp.causes.get("cloud_accounts") or "the cloud accounts file was not read"
        )
        return [
            _unread_row(
                "cloud_credit",
                "all",
                "Claude cloud-session credit",
                inp.cloud_accounts_source,
                cause,
            )
        ]
    rows: list[ModelFloorLimitRow] = []
    for a in inp.cloud_accounts:
        unread = "" if a.credit_remaining is not None else "no credit reading recorded"
        rows.append(
            _row(
                "cloud_credit",
                a.name,
                a.name,
                f"{inp.cloud_accounts_source}#{a.name}.credit",
                a.credit_read_at,
                now,
                cycle,
                (
                    _val("credit_remaining_usd", a.credit_remaining, "USD"),
                    _val("credit_expires", a.credit_expires or None),
                    _val("credit_source", a.credit_source or None),
                ),
                unread=unread,
            )
        )
    return rows


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    days = calendar.monthrange(now.year, now.month)[1]
    return start, start + timedelta(days=days)


def _api_rows(inp: ModelFloorLimitsInput) -> list[ModelFloorLimitRow]:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    budget = inp.config.api_budget_usd
    month = f"{now:%Y-%m}"
    meter = inp.api_meter
    source = inp.api_meter_source
    if meter is None:
        cause = inp.causes.get("api_meter") or "the API credit meter was not read"
        names = inp.api_key_names or ("anthropic",)
        return [
            _unread_row("api_credit", n, f"Anthropic API key {n}", source, cause)
            for n in sorted(names)
        ]
    by_name = {k.name: k for k in meter.keys}
    rows: list[ModelFloorLimitRow] = []
    start, end = _month_bounds(now)
    elapsed = now - start
    for name in sorted({*by_name, *inp.api_key_names}):
        key = by_name.get(name)
        unread = ""
        if key is None:
            unread = "the meter does not list this key"
        elif key.month != month:
            unread = f"the meter's reading is for month {key.month or 'unstated'}, not {month}"
        elif key.month_spend_usd is None:
            unread = "the meter lists the key with no spend figure"
        spend = key.month_spend_usd if key is not None else None
        projected_spend: float | None = None
        if spend is not None and elapsed >= timedelta(
            hours=inp.config.projection_min_elapsed_hours
        ):
            projected_spend = spend * ((end - start) / elapsed)
        values = (
            _val("month_spend_usd", spend, "USD"),
            _val("budget_usd", budget, "USD"),
            _val("remaining_usd", None if spend is None else budget - spend, "USD"),
            _val("projected_month_end_spend_usd", projected_spend, "USD"),
            _val(
                "projected_unspent_usd",
                None if projected_spend is None else budget - projected_spend,
                "USD",
            ),
        )
        rows.append(
            _row(
                "api_credit",
                name,
                f"Anthropic API key {name}",
                source,
                meter.read_at,
                now,
                cycle,
                values,
                unread=unread,
            )
        )
    return rows


def _usage_row(
    kind: LimitKind,
    name: str,
    reading: ModelFloorUsageWindow | None,
    source: str,
    now: datetime,
    cycle: int,
    absent: str,
) -> ModelFloorLimitRow:
    if reading is None:
        return _unread_row(kind, kind, name, source, absent)
    unread = (
        "" if reading.used_pct is not None else "the reading carries no usage figure"
    )
    return _row(
        kind,
        kind,
        name,
        source,
        reading.read_at,
        now,
        cycle,
        (
            _val("window", reading.window or None),
            _val("used_pct", reading.used_pct, "%"),
            _val("resets_at", reading.resets_at or None),
        ),
        unread=unread,
    )


def _engine_rows(inp: ModelFloorLimitsInput) -> list[ModelFloorLimitRow]:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    engines: ModelFloorEngineReadings | None = inp.engines
    source = inp.engines_source
    absent = inp.causes.get("engines") or "the engine readings were not read"
    if engines is None:
        return [
            _unread_row(
                "codex", "codex", "Codex (ChatGPT) usage window", source, absent
            ),
            _unread_row("glm", "glm", "GLM coding plan", source, absent),
            _unread_row("lab_model", "all", "Lab model rungs", source, absent),
        ]
    rows = [
        _usage_row(
            "codex",
            "Codex (ChatGPT) usage window",
            engines.codex,
            f"{source}#codex",
            now,
            cycle,
            "no Codex reading in the engine readings",
        ),
        _usage_row(
            "glm",
            "GLM coding plan",
            engines.glm,
            f"{source}#glm",
            now,
            cycle,
            "no GLM reading in the engine readings",
        ),
    ]
    if not engines.lab_models:
        rows.append(
            _unread_row(
                "lab_model",
                "all",
                "Lab model rungs",
                f"{source}#lab_models",
                "no lab model rung in the engine readings",
            )
        )
    for rung in sorted(engines.lab_models, key=lambda r: r.name):
        rows.append(
            _row(
                "lab_model",
                rung.name,
                rung.name,
                f"{source}#lab_models.{rung.name}",
                rung.read_at,
                now,
                cycle,
                (
                    _val("available", rung.available),
                    _val("last_smoke_at", rung.last_smoke_at or None),
                ),
                unread=""
                if rung.available is not None
                else "availability was not read",
            )
        )
    return rows


def _pool_counts(
    pool: ModelFloorHostTokenPool, now: datetime
) -> tuple[int, int, int, str]:
    """(usable, limited, revoked, latest limited-until). A revoked token is revoked whatever else it says, and a
    limited one with no readable end is limited: neither is ever counted as capacity."""
    usable = limited = revoked = 0
    latest = ""
    for token in pool.tokens:
        if token.state == "revoked":
            revoked += 1
            continue
        until = _moment(token.limited_until)
        held = (token.state == "limited" and until is None) or (
            until is not None and until > now
        )
        if held:
            limited += 1
            if until is not None and token.limited_until > latest:
                latest = token.limited_until
        else:
            usable += 1
    return usable, limited, revoked, latest


def _pool_rows(inp: ModelFloorLimitsInput) -> list[ModelFloorLimitRow]:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    source = inp.token_pool_source
    if inp.token_pool is None:
        cause = (
            inp.causes.get("token_pool") or "the Claude token pool reading was not read"
        )
        return [_unread_row("account_pool", "all", "Claude login pool", source, cause)]
    rows: list[ModelFloorLimitRow] = []
    for pool in sorted(inp.token_pool, key=lambda p: p.host):
        usable, limited, revoked, latest = _pool_counts(pool, now)
        rows.append(
            _row(
                "account_pool",
                pool.host,
                pool.host,
                f"{source}#{pool.host}",
                pool.read_at,
                now,
                cycle,
                (
                    _val("usable", usable),
                    _val("limited", limited),
                    _val("limited_until", latest),
                    _val("revoked", revoked),
                ),
                unread="" if pool.tokens else "the pool lists no token for the host",
            )
        )
    return rows


def _idle(inp: ModelFloorLimitsInput) -> ModelFloorLimitsIdle:
    cycle, now = inp.config.publish_cycle_seconds, inp.now
    reading = inp.idle
    if reading is None:
        return ModelFloorLimitsIdle(
            state="unread",
            day="",
            hosts=(),
            idle_total=None,
            idle_unread=0,
            idle_slot_hours=None,
            unread_minutes=0.0,
            basis=inp.causes.get("idle") or "the merged-and-running count was not read",
        )
    hours: float | None = None
    unread_minutes = 0.0
    carry = inp.idle_carry
    if carry is not None and carry.day == reading.day:
        gap = max(0.0, (now - carry.generated_at).total_seconds())
        counted = (
            min(gap, float(ACCRUAL_CYCLES * cycle))
            if carry.idle_total is not None
            else 0.0
        )
        hours = (carry.idle_slot_hours or 0.0) + (
            carry.idle_total or 0
        ) * counted / 3600
        unread_minutes = carry.unread_minutes + (gap - counted) / 60
    return ModelFloorLimitsIdle(
        state="read",
        day=reading.day,
        hosts=tuple(
            ModelFloorIdleHostOut(
                host=h.host, state=h.state, idle=h.idle, slots=h.slots
            )
            for h in reading.hosts
        ),
        idle_total=reading.idle_total,
        idle_unread=reading.idle_unread,
        idle_slot_hours=None if hours is None else round(hours, 3),
        unread_minutes=round(unread_minutes, 1),
        basis=IDLE_BASIS
        if hours is not None
        else "first reading of the day: no interval has accrued yet",
    )


def build_limits(inp: ModelFloorLimitsInput) -> ModelFloorLimits:
    """The Limits section: subscription, cloud credit, API credit, Codex, GLM, lab rungs and token pools, then idle."""
    return ModelFloorLimits(
        generated_at=inp.now.astimezone(UTC).isoformat(),
        publish_cycle_seconds=inp.config.publish_cycle_seconds,
        rows=(
            *_subscription_rows(inp),
            *_credit_rows(inp),
            *_api_rows(inp),
            *_engine_rows(inp),
            *_pool_rows(inp),
        ),
        idle=_idle(inp),
    )
