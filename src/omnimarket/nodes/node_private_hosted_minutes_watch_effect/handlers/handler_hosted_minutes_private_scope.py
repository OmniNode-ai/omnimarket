# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Private-repository scope: classification, threshold, alert and STATUS row text."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal

from ..models import (
    ModelHostedMinutesOverlay,
    ModelRepoDayMinutes,
    ModelUncountedRepo,
)


def _safe(value: str) -> str:
    return value.replace("|", "/").replace("\r", " ").replace("\n", " ")


def format_minutes(value: float) -> str:
    """Plain decimal with at most two places, never scientific notation."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


_minutes = format_minutes


def alert_threshold(overlay: ModelHostedMinutesOverlay) -> Decimal:
    """Minutes at or above which the alert fires, from the overlay alone."""
    return Decimal(overlay.included_minutes) * Decimal(str(overlay.alert_fraction))


def percent_used(private_month: float, overlay: ModelHostedMinutesOverlay) -> float:
    return round(private_month * 100 / overlay.included_minutes, 2)


def classify_repositories(
    org: str,
    month_minutes: Mapping[str, float],
    read_visibility: Callable[[str, str], str],
) -> tuple[frozenset[str], tuple[ModelUncountedRepo, ...]]:
    """One visibility read per repository; only a reported 'private' is counted."""
    private: set[str] = set()
    uncounted: list[ModelUncountedRepo] = []
    for repository in sorted(month_minutes):
        try:
            visibility = read_visibility(org, repository).strip().lower()
        except Exception as exc:
            uncounted.append(
                ModelUncountedRepo(
                    repository=repository,
                    minutes=month_minutes[repository],
                    reason="unreadable",
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            continue
        if visibility == "private":
            private.add(repository)
        else:
            uncounted.append(
                ModelUncountedRepo(
                    repository=repository,
                    minutes=month_minutes[repository],
                    reason=_safe(visibility) or "unclassified",
                )
            )
    return frozenset(private), tuple(uncounted)


def render_private_alert(
    org: str,
    day: date,
    private_month: float,
    overlay: ModelHostedMinutesOverlay,
    private_by_repo: Mapping[str, float],
) -> str:
    repos = ", ".join(
        f"{_safe(name)} {_minutes(minutes)} min"
        for name, minutes in sorted(
            private_by_repo.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )
    return (
        f"GitHub-hosted Actions minutes on private repositories, month to {day} UTC: "
        f"{_minutes(private_month)} of {overlay.included_minutes} included minutes used "
        f"({format_minutes(percent_used(private_month, overlay))} percent). "
        f"Private repositories by minutes: {repos or 'none'}. Org {_safe(org)}."
    )


def render_private_status_row(
    stamp: str,
    day: date,
    day_rows: Sequence[ModelRepoDayMinutes],
    total_day_minutes: float,
    alert: str,
    items_read: int,
    private_by_repo: Mapping[str, float],
    private_month: float,
    overlay: ModelHostedMinutesOverlay,
    uncounted: Sequence[ModelUncountedRepo],
) -> str:
    repos = (
        ",".join(f"{_safe(row.repository)}:{_minutes(row.minutes)}" for row in day_rows)
        or "none"
    )
    month = (
        ",".join(
            f"{_safe(r)}:{_minutes(m)}" for r, m in sorted(private_by_repo.items())
        )
        or "none"
    )
    uncounted_total = sum(item.minutes for item in uncounted)
    uncounted_repos = (
        ",".join(
            f"{_safe(item.repository)}:{_minutes(item.minutes)}:{item.reason}"
            for item in uncounted
        )
        or "none"
    )
    return (
        f"{_safe(stamp)} | STATUS | lane=private-hosted-minutes-watch | ticket=OMN-17427 | "
        f"actor=launchd:private-hosted-minutes-watch | day={day} | "
        f"hosted_minutes={_minutes(total_day_minutes)} | repos={repos} | alert={_safe(alert)} | "
        f"private_month_minutes={_minutes(private_month)} | included={overlay.included_minutes} | "
        f"percent_used={format_minutes(percent_used(private_month, overlay))} | "
        f"uncounted={_minutes(uncounted_total)} | "
        f"private-repository GitHub-hosted Actions minutes for {day} UTC from one billing "
        f"usage read ({items_read} usage items); private month to date by repository: {month}; "
        f"uncounted by repository (repository:minutes:reason): {uncounted_repos}"
    )
