# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure classification, aggregation and rendering of GitHub billing usage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from math import isfinite
from typing import Any

from ..models import ModelRepoDayMinutes


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if isfinite(number) else 0.0


def billable_hosted_minutes(items: list[dict[str, Any]]) -> list[ModelRepoDayMinutes]:
    """Keep positive Actions hosted minutes carrying a positive gross amount."""
    if not isinstance(items, list):
        raise ValueError("usageItems must be a list")
    quantities: dict[tuple[str, date], float] = {}
    skus: dict[tuple[str, date], set[str]] = {}
    workflows: dict[tuple[str, date], set[str]] = {}
    for item in items:
        product = str(item.get("product", "")).lower()
        unit = str(item.get("unitType", "")).lower()
        sku = str(item.get("sku", ""))
        repository = item.get("repositoryName")
        quantity = _number(item.get("quantity"))
        if (
            product != "actions"
            or unit != "minutes"
            or "self_hosted" in sku.lower()
            or "self-hosted" in sku.lower()
            or quantity <= 0
            or _number(item.get("grossAmount")) <= 0
            or not isinstance(repository, str)
            or not repository.strip()
        ):
            continue
        day = date.fromisoformat(str(item.get("date", ""))[:10])
        key = (repository, day)
        quantities[key] = quantities.get(key, 0.0) + quantity
        skus.setdefault(key, set()).add(sku)
        names = workflows.setdefault(key, set())
        for field in ("workflowPath", "workflowName", "workflow"):
            workflow = item.get(field)
            if isinstance(workflow, str) and workflow:
                names.add(workflow)
    return [
        ModelRepoDayMinutes(
            repository=repository,
            day=day,
            minutes=quantities[(repository, day)],
            skus=tuple(sorted(skus[(repository, day)])),
            workflows=tuple(sorted(workflows[(repository, day)])),
        )
        for repository, day in sorted(quantities, key=lambda key: (key[1], key[0]))
    ]


def month_by_repo(rows: Iterable[ModelRepoDayMinutes]) -> dict[str, float]:
    """Sum all supplied days by repository, sorted and rounded for reporting."""
    totals: dict[str, float] = {}
    for row in rows:
        totals[row.repository] = totals.get(row.repository, 0.0) + row.minutes
    return {key: round(totals[key], 2) for key in sorted(totals)}


def daily_totals(rows: Iterable[ModelRepoDayMinutes]) -> dict[str, float]:
    """Sum all repositories by ISO UTC day, sorted and rounded for reporting."""
    totals: dict[str, float] = {}
    for row in rows:
        key = row.day.isoformat()
        totals[key] = totals.get(key, 0.0) + row.minutes
    return {key: round(totals[key], 2) for key in sorted(totals)}


def _safe(value: str) -> str:
    return value.replace("|", "/").replace("\r", " ").replace("\n", " ")


def _minutes(value: float) -> str:
    return f"{round(value, 2):g}"


def render_alert(org: str, day: date, day_rows: Sequence[ModelRepoDayMinutes]) -> str:
    """One line with the checked day's repositories and any workflow evidence."""
    repos = []
    for row in day_rows:
        text = f"{_safe(row.repository)} {_minutes(row.minutes)} min"
        if row.workflows:
            text += f" (workflows: {', '.join(_safe(name) for name in row.workflows)})"
        repos.append(text)
    total = _minutes(sum(row.minutes for row in day_rows))
    return (
        f"GitHub-hosted Actions minutes on private repositories, {day} UTC (rule: zero): "
        f"{', '.join(repos)}; total {total} min. Org {_safe(org)}."
    )


def render_status_row(
    stamp: str,
    day: date,
    day_rows: Sequence[ModelRepoDayMinutes],
    total_day_minutes: float,
    alert: str,
    items_read: int,
    month_minutes_by_repo: Mapping[str, float],
) -> str:
    """One rolling-ledger STATUS row with no data-supplied pipe delimiters."""
    repos = (
        ",".join(f"{_safe(row.repository)}:{_minutes(row.minutes)}" for row in day_rows)
        or "none"
    )
    month = (
        ",".join(
            f"{_safe(repo)}:{_minutes(minutes)}"
            for repo, minutes in sorted(month_minutes_by_repo.items())
        )
        or "none"
    )
    return (
        f"{_safe(stamp)} | STATUS | lane=private-hosted-minutes-watch | ticket=OMN-17427 | "
        f"actor=launchd:private-hosted-minutes-watch | day={day} | "
        f"hosted_minutes={_minutes(total_day_minutes)} | repos={repos} | alert={_safe(alert)} | "
        f"private-repository GitHub-hosted Actions minutes for {day} UTC from one billing "
        f"usage read ({items_read} usage items); month to date by repository: {month}"
    )
