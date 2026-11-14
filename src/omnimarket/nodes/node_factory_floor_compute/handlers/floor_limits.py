# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Limits panel: one row per account, key, engine, rung and host token pool, each with its source and read time.

A reading never read, or read longer ago than one publish cycle, is UNKNOWN and carries no value at all: a missing
reading is never drawn as zero. A revoked token is counted as revoked and never as capacity (OMN-22010). The cycle,
the default API budget and the credit expiry come from the contract's config block; this module holds none.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorCloudAccount,
    ModelFloorLimitsConfig,
    ModelLimitAccountVerdict,
    ModelLimitApiKey,
    ModelLimitEngine,
    ModelLimitLabRung,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    LimitKind,
    ModelFloorLimitRow,
    ModelFloorLimits,
)

Values = dict[str, float | int | str | bool]


def _parse(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fresh(read_at: str, now: datetime, cfg: ModelFloorLimitsConfig) -> bool:
    """True when the reading was taken within one publish cycle of ``now``."""
    when = _parse(read_at)
    return when is not None and now - when <= timedelta(
        minutes=cfg.publish_cycle_minutes
    )


def _why_not_fresh(read_at: str, cfg: ModelFloorLimitsConfig) -> str:
    if not _parse(read_at):
        return "never read" if not read_at else "read time is not a timestamp"
    return f"last read {read_at}, older than one publish cycle ({cfg.publish_cycle_minutes} min)"


def _row(
    kind: LimitKind,
    name: str,
    source: str,
    read_at: str,
    values: Values,
    *,
    unknown_reason: str = "",
    unknown_fields: tuple[str, ...] = (),
) -> ModelFloorLimitRow:
    """A read row when it holds a value and no reason against it; else UNKNOWN with no value."""
    if unknown_reason or not values:
        return ModelFloorLimitRow(
            row_id=f"{kind}:{name}",
            kind=kind,
            name=name,
            state="UNKNOWN",
            source=source,
            read_at=read_at,
            unknown_reason=unknown_reason or "the source gave no value",
            values={},
        )
    return ModelFloorLimitRow(
        row_id=f"{kind}:{name}",
        kind=kind,
        name=name,
        state="read",
        source=source,
        read_at=read_at,
        unknown_fields=unknown_fields,
        values=values,
    )


def _limited_until(
    pools: tuple[ModelLimitTokenPool, ...] | None,
    label: str,
    now: datetime,
    cfg: ModelFloorLimitsConfig,
) -> tuple[str, bool]:
    """(the latest future limit on a token of this label, whether the pools were read fresh)."""
    if pools is None:
        return "", False
    readable = [p for p in pools if _fresh(p.read_at, now, cfg)]
    if not readable:
        return "", False
    stamps = [
        t.limited_until
        for p in readable
        for t in p.tokens
        if t.label == label
        and not t.revoked
        and (until := _parse(t.limited_until)) is not None
        and until > now
    ]
    return (max(stamps) if stamps else ""), True


def _subscription_rows(
    accounts: Sequence[ModelFloorCloudAccount],
    pools: tuple[ModelLimitTokenPool, ...] | None,
    now: datetime,
    cfg: ModelFloorLimitsConfig,
) -> list[ModelFloorLimitRow]:
    source = "cloud-accounts.json rate"
    rows: list[ModelFloorLimitRow] = []
    for a in sorted(accounts, key=lambda a: a.name):
        if not _fresh(a.rate_read_at, now, cfg):
            rows.append(
                _row(
                    "claude_subscription",
                    a.name,
                    source,
                    a.rate_read_at,
                    {},
                    unknown_reason=_why_not_fresh(a.rate_read_at, cfg),
                )
            )
            continue
        values: Values = {}
        missing: list[str] = []
        for key, value in (
            ("five_hour_utilization", a.five_hour_utilization),
            ("five_hour_resets_at", a.five_hour_resets_at),
            ("seven_day_utilization", a.seven_day_utilization),
            ("seven_day_resets_at", a.seven_day_resets_at),
        ):
            if value is None or value == "":
                missing.append(key)
            else:
                values[key] = value
        until, pool_read = _limited_until(pools, a.name, now, cfg)
        if until:
            values["limited_until"] = until
        elif not pool_read:
            missing.append("limited_until")
        rows.append(
            _row(
                "claude_subscription",
                a.name,
                source,
                a.rate_read_at,
                values,
                unknown_fields=tuple(missing),
            )
        )
    return rows


def _credit_rows(
    accounts: Sequence[ModelFloorCloudAccount],
    now: datetime,
    cfg: ModelFloorLimitsConfig,
) -> list[ModelFloorLimitRow]:
    expiry = datetime.fromisoformat(cfg.cloud_credit_expiry).replace(tzinfo=UTC)
    rows: list[ModelFloorLimitRow] = []
    for a in sorted(accounts, key=lambda a: a.name):
        source = a.credit_source or "cloud-accounts.json credit"
        if a.credit_remaining is None or not _fresh(a.credit_read_at, now, cfg):
            reason = (
                "no credit reading in the record"
                if a.credit_remaining is None
                else _why_not_fresh(a.credit_read_at, cfg)
            )
            rows.append(
                _row(
                    "cloud_credit",
                    a.name,
                    source,
                    a.credit_read_at,
                    {},
                    unknown_reason=reason,
                )
            )
            continue
        rows.append(
            _row(
                "cloud_credit",
                a.name,
                source,
                a.credit_read_at,
                {
                    "remaining": a.credit_remaining,
                    "expires_on": cfg.cloud_credit_expiry,
                    "expired": now >= expiry + timedelta(days=1),
                },
            )
        )
    return rows


def _month_fraction(now: datetime) -> float:
    """How much of the calendar month (UTC) has passed at ``now``."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = calendar.monthrange(now.year, now.month)[1]
    return (now - start) / timedelta(days=days)


def _api_rows(
    keys: Sequence[ModelLimitApiKey], now: datetime, cfg: ModelFloorLimitsConfig
) -> list[ModelFloorLimitRow]:
    rows: list[ModelFloorLimitRow] = []
    for k in sorted(keys, key=lambda k: k.name):
        if k.month_spend_usd is None or not _fresh(k.read_at, now, cfg):
            reason = (
                "the meter gave no spend"
                if k.month_spend_usd is None
                else _why_not_fresh(k.read_at, cfg)
            )
            rows.append(
                _row(
                    "api_credit",
                    k.name,
                    k.source,
                    k.read_at,
                    {},
                    unknown_reason=reason,
                )
            )
            continue
        budget = k.budget_usd if k.budget_usd is not None else cfg.api_budget_usd
        values: Values = {
            "month_spend_usd": k.month_spend_usd,
            "budget_usd": budget,
            "remaining_usd": budget - k.month_spend_usd,
        }
        missing: list[str] = []
        fraction = _month_fraction(now)
        if fraction > 0:
            projected = k.month_spend_usd / fraction
            values["projected_month_spend_usd"] = projected
            values["projected_unspent_usd"] = budget - projected
        else:
            missing = ["projected_month_spend_usd", "projected_unspent_usd"]
        rows.append(
            _row(
                "api_credit",
                k.name,
                k.source,
                k.read_at,
                values,
                unknown_fields=tuple(missing),
            )
        )
    return rows


def _engine_row(
    kind: LimitKind,
    engine: ModelLimitEngine | None,
    now: datetime,
    cfg: ModelFloorLimitsConfig,
) -> ModelFloorLimitRow:
    if engine is None:
        return _row(
            kind,
            kind,
            f"no reading ({kind})",
            "",
            {},
            unknown_reason="the source could not be read",
        )
    if engine.used_pct is None or not _fresh(engine.read_at, now, cfg):
        reason = (
            "the source gave no usage"
            if engine.used_pct is None
            else _why_not_fresh(engine.read_at, cfg)
        )
        return _row(
            kind, kind, engine.source, engine.read_at, {}, unknown_reason=reason
        )
    values: Values = {"used_pct": engine.used_pct}
    missing: list[str] = []
    for key, value in (("window", engine.window), ("resets_at", engine.resets_at)):
        if value:
            values[key] = value
        else:
            missing.append(key)
    return _row(
        kind,
        kind,
        engine.source,
        engine.read_at,
        values,
        unknown_fields=tuple(missing),
    )


def _rung_rows(
    rungs: Sequence[ModelLimitLabRung], now: datetime, cfg: ModelFloorLimitsConfig
) -> list[ModelFloorLimitRow]:
    rows: list[ModelFloorLimitRow] = []
    for r in sorted(rungs, key=lambda r: r.name):
        if r.available is None or not _fresh(r.read_at, now, cfg):
            reason = (
                "the source gave no availability"
                if r.available is None
                else _why_not_fresh(r.read_at, cfg)
            )
            rows.append(
                _row(
                    "lab_model_rung",
                    r.name,
                    r.source,
                    r.read_at,
                    {},
                    unknown_reason=reason,
                )
            )
            continue
        values: Values = {"available": r.available}
        missing: list[str] = []
        if r.last_smoke_at:
            values["last_smoke_at"] = r.last_smoke_at
        else:
            missing.append("last_smoke_at")
        if r.last_smoke_ok is not None:
            values["last_smoke_ok"] = r.last_smoke_ok
        else:
            missing.append("last_smoke_ok")
        rows.append(
            _row(
                "lab_model_rung",
                r.name,
                r.source,
                r.read_at,
                values,
                unknown_fields=tuple(missing),
            )
        )
    return rows


ACCOUNT_COUNT_KEYS = (
    "accounts_usable",
    "accounts_draining",
    "accounts_limited",
    "accounts_unknown",
)


def _account_counts(
    pool: ModelLimitTokenPool,
    verdicts: Sequence[ModelLimitAccountVerdict] | None,
    now: datetime,
) -> Values:
    """The host's accounts by usage verdict; empty when no verdict was made, so the counts read UNKNOWN, never zero.

    A revoked token is not an account in use (it is in ``revoked``). A limit mark on this host's token makes it limited;
    otherwise its account's verdict decides, and an account with no verdict (no reading, a stale one, or none judged)
    is unknown and never usable. ``accounts_usable`` includes the draining accounts: they take work until they limit.
    """
    if verdicts is None:
        return {}
    by_label = {v.label: v.verdict for v in verdicts}
    counts = dict.fromkeys(ACCOUNT_COUNT_KEYS, 0)
    for token in pool.tokens:
        if token.revoked:
            continue
        mark = _parse(token.limited_until)
        verdict = (
            "limited"
            if mark is not None and mark > now
            else by_label.get(token.label, "unknown")
        )
        if verdict == "ok":
            counts["accounts_usable"] += 1
        elif verdict == "drain":
            counts["accounts_usable"] += 1
            counts["accounts_draining"] += 1
        elif verdict == "limited":
            counts["accounts_limited"] += 1
        else:
            counts["accounts_unknown"] += 1
    return dict(counts)


def _pool_rows(
    pools: Sequence[ModelLimitTokenPool],
    now: datetime,
    cfg: ModelFloorLimitsConfig,
    verdicts: Sequence[ModelLimitAccountVerdict] | None = None,
) -> list[ModelFloorLimitRow]:
    rows: list[ModelFloorLimitRow] = []
    for p in sorted(pools, key=lambda p: p.host):
        if not _fresh(p.read_at, now, cfg):
            rows.append(
                _row(
                    "host_account_pool",
                    p.host,
                    p.source,
                    p.read_at,
                    {},
                    unknown_reason=_why_not_fresh(p.read_at, cfg),
                )
            )
            continue
        revoked = [t for t in p.tokens if t.revoked]
        limited_stamps = [
            t.limited_until
            for t in p.tokens
            if not t.revoked
            and (until := _parse(t.limited_until)) is not None
            and until > now
        ]
        limited = len(limited_stamps)
        values: Values = {
            "total": len(p.tokens),
            "revoked": len(revoked),
            "limited": limited,
            # A revoked token is out for good: it is never usable, whatever its limit time says.
            "usable": len(p.tokens) - len(revoked) - limited,
        }
        if limited_stamps:
            values["limited_until"] = max(limited_stamps)
        accounts = _account_counts(p, verdicts, now)
        values.update(accounts)
        rows.append(
            _row(
                "host_account_pool",
                p.host,
                p.source,
                p.read_at,
                values,
                unknown_fields=() if accounts else ACCOUNT_COUNT_KEYS,
            )
        )
    return rows


def compute_limits(request: ModelFactoryFloorRequest) -> ModelFloorLimits:
    """The Limits panel from the facts in ``request``; no clock beyond ``generated_at``, no I/O."""
    cfg = request.config.limits
    now = request.generated_at
    facts = request.limits
    unread: list[str] = []
    rows: list[ModelFloorLimitRow] = []

    if request.cloud_accounts is None:
        unread.append("cloud_accounts")
    else:
        rows += _subscription_rows(request.cloud_accounts, facts.token_pools, now, cfg)
        rows += _credit_rows(request.cloud_accounts, now, cfg)
    if facts.api_keys is None:
        unread.append("api_keys")
    else:
        rows += _api_rows(facts.api_keys, now, cfg)
    rows.append(_engine_row("codex", facts.codex, now, cfg))
    rows.append(_engine_row("glm", facts.glm, now, cfg))
    if facts.lab_rungs is None:
        unread.append("lab_rungs")
    else:
        rows += _rung_rows(facts.lab_rungs, now, cfg)
    if facts.token_pools is None:
        unread.append("token_pools")
    else:
        rows += _pool_rows(facts.token_pools, now, cfg, facts.account_verdicts)

    idle_fresh = facts.idle_slot_hours is not None and _fresh(
        facts.idle_slot_hours_read_at, now, cfg
    )
    # Order: the rows come out grouped by kind, in the order the panel draws them.
    order: list[LimitKind] = [
        "claude_subscription",
        "cloud_credit",
        "api_credit",
        "codex",
        "glm",
        "lab_model_rung",
        "host_account_pool",
    ]
    rows.sort(key=lambda r: order.index(r.kind))
    return ModelFloorLimits(
        publish_cycle_minutes=cfg.publish_cycle_minutes,
        rows=tuple(rows),
        unread_sources=tuple(unread),
        idle_slot_hours=facts.idle_slot_hours if idle_fresh else None,
        idle_slot_hours_read_at=facts.idle_slot_hours_read_at,
    )
