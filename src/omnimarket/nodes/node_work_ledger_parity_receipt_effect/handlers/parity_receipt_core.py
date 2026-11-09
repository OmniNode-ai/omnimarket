# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure decisions of the daily parity receipt writer (OMN-23251, from OMN-20536).

The skip rule: a day is owed a receipt when the ledger holds none for it, or when the newest receipt for it shows
``missing`` or ``unexplained`` above 0 (or does not show them at all, which proves nothing). A day whose newest
receipt shows both at 0 is skipped. Newest is the latest timestamp cell, and the later line on a tie.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal

from omnimarket.nodes.node_work_ledger_parity_receipt_effect.models.model_parity_receipt import (
    ModelParityReceiptSettings,
)

Owed = Literal["no-receipt", "newest-unhealthy", "newest-clean"]


@dataclass(frozen=True)
class ReceiptRow:
    stamp: str
    day: str
    missing: int | None
    unexplained: int | None
    position: int

    @property
    def clean(self) -> bool:
        return self.missing == 0 and self.unexplained == 0


def row_pattern(lane: str) -> re.Pattern[str]:
    """One receipt line of the lane: the timestamp, the lane cell and the day cell."""
    return re.compile(
        r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| STATUS \| lane="
        + re.escape(lane)
        + r" \| .*\| day=(?P<day>\d{4}-\d{2}-\d{2}) \|"
    )


def _count(line: str, cell: str) -> int | None:
    found = re.search(rf"\| {cell}=(\d+) \|", line)
    return int(found.group(1)) if found else None


def receipt_rows(text: str, lane: str) -> list[ReceiptRow]:
    pattern = row_pattern(lane)
    rows: list[ReceiptRow] = []
    for position, line in enumerate(text.splitlines()):
        found = pattern.match(line)
        if found is None:
            continue
        rows.append(
            ReceiptRow(
                stamp=found.group("stamp"),
                day=found.group("day"),
                missing=_count(line, "missing"),
                unexplained=_count(line, "unexplained"),
                position=position,
            )
        )
    return rows


def newest_receipt(text: str, lane: str, day: str) -> ReceiptRow | None:
    rows = [row for row in receipt_rows(text, lane) if row.day == day]
    return max(rows, key=lambda row: (row.stamp, row.position), default=None)


def receipt_owed(text: str, lane: str, day: str) -> Owed:
    newest = newest_receipt(text, lane, day)
    if newest is None:
        return "no-receipt"
    return "newest-clean" if newest.clean else "newest-unhealthy"


def receipt_day(now: datetime, settings: ModelParityReceiptSettings) -> str | None:
    """The UTC day a run at ``now`` owes a receipt for, or None before the not-before time."""
    now = now.astimezone(UTC)
    if now.time() < time(settings.not_before_hour, settings.not_before_minute):
        return None
    return (now.date() - timedelta(days=1)).isoformat()


def key_values(text: str) -> dict[str, str]:
    """KEY=VALUE lines of an env file, read as text and never sourced."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def overlay_values(text: str, settings: ModelParityReceiptSettings) -> dict[str, str]:
    keys = (
        settings.overlay_host_key,
        settings.overlay_database_key,
        settings.overlay_user_key,
    )
    pattern = re.compile(
        r"^(" + "|".join(re.escape(k) for k in keys) + r'):\s*"?([^"#]*?)"?\s*$'
    )
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match is not None:
            found[match.group(1)] = match.group(2).strip()
    return found


def build_dsn(
    overlay_text: str,
    env_file_text: str,
    host_environment: Mapping[str, str],
    port: int,
    settings: ModelParityReceiptSettings,
) -> str:
    """The projection DSN; raises KeyError when a value the overlay or the env file must hold is absent."""
    overlay = overlay_values(overlay_text, settings)
    lab = key_values(env_file_text)
    host_variable = overlay[settings.overlay_host_key]
    host = (host_environment.get(host_variable) or lab[host_variable]).rsplit("@", 1)[
        -1
    ]
    user = overlay[settings.overlay_user_key]
    password = lab[settings.env_password_key]
    database = overlay[settings.overlay_database_key]
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"
