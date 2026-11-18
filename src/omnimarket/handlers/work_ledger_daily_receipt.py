# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The exact UTC daily receipt: the ledger table against the accepted-row topic, nothing else (OMN-23153).

Pure: two lists of one day's rows in, one receipt out. The day's cutoff is the contract's fixed UTC
time on the next day, and the window closes ``close_minutes`` after it; the input manifest is frozen
at the close, so a row first seen later is counted as late and left out of both sides. Identical
duplicates in a source are dropped by key; one key under two sequences fails the window.
The receipt carries the verb comparison of the day (``parity_verbs``); it is reported beside the
rows and does not change the row verdict, the way the row-kind cells of OMN-21885 do not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources

import yaml

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_daily_receipt import (
    EnumDailyReceiptState,
    ModelDailyReceipt,
    ModelDailyReceiptConfig,
    ModelDailyReceiptRequest,
    ModelDailyReceiptRow,
)
from omnimarket.nodes.node_projection_work_ledger.parity_verbs import format_verb_diff


def load_daily_receipt_config() -> ModelDailyReceiptConfig:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_projection_work_ledger")
        .joinpath("contract.yaml")
        .read_text()
    )
    return ModelDailyReceiptConfig.model_validate(contract["daily_receipt"])


def receipt_window(
    day: str, config: ModelDailyReceiptConfig
) -> tuple[datetime, datetime, datetime]:
    """``(day_start, cutoff, closes_at)`` for UTC ``day``. Pure."""
    start = datetime.combine(date.fromisoformat(day), time(0, 0), tzinfo=UTC)
    hour, minute = (int(part) for part in config.cutoff_utc.split(":"))
    cutoff = start + timedelta(days=1, hours=hour, minutes=minute)
    return start, cutoff, cutoff + timedelta(minutes=config.close_minutes)


def _freeze(
    rows: Iterable[ModelDailyReceiptRow],
    start: datetime,
    end: datetime,
    closes_at: datetime,
) -> tuple[dict[str, ModelDailyReceiptRow], set[str], int]:
    """The day's rows of one source by key, the keys that conflict, and the late count."""
    kept: dict[str, ModelDailyReceiptRow] = {}
    conflicts: set[str] = set()
    late = 0
    for row in rows:
        if not start <= row.row_ts < end:
            continue
        if row.seen_at is not None and row.seen_at > closes_at:
            late += 1
            continue
        seen = kept.get(row.row_id)
        if seen is None:
            kept[row.row_id] = row
        elif seen.ledger_seq is None:
            kept[row.row_id] = (
                row  # an unsequenced copy takes its sequenced twin's place
            )
        elif row.ledger_seq is not None and row.ledger_seq != seen.ledger_seq:
            conflicts.add(row.row_id)
    return kept, conflicts, late


def _order(rows: Mapping[str, ModelDailyReceiptRow]) -> tuple[str, ...]:
    return tuple(
        key
        for key, _ in sorted(
            rows.items(),
            key=lambda kv: (kv[1].ledger_seq is None, kv[1].ledger_seq or 0, kv[0]),
        )
    )


def build_daily_receipt(
    request: ModelDailyReceiptRequest, config: ModelDailyReceiptConfig
) -> ModelDailyReceipt:
    """The receipt for ``request.day``; NOT-CLOSED before the window closes."""
    start, cutoff, closes_at = receipt_window(request.day, config)
    if request.now < closes_at:
        return ModelDailyReceipt(
            day=request.day,
            state=EnumDailyReceiptState.NOT_CLOSED,
            cutoff_at=cutoff,
            closes_at=closes_at,
            verb_diff=request.verb_diff,
        )
    end = start + timedelta(days=1)
    table, table_conflicts, table_late = _freeze(
        request.table_rows, start, end, closes_at
    )
    topic, topic_conflicts, topic_late = _freeze(
        request.topic_rows, start, end, closes_at
    )
    missing = tuple(sorted(topic.keys() - table.keys()))
    extra = tuple(sorted(table.keys() - topic.keys()))
    mismatched = tuple(
        sorted(
            key
            for key in table.keys() & topic.keys()
            if (table[key].ledger_seq, table[key].row_ts)
            != (topic[key].ledger_seq, topic[key].row_ts)
        )
    )
    conflicts = tuple(sorted(table_conflicts | topic_conflicts))
    exact = (
        bool(table)
        and bool(topic)
        and not (missing or extra or mismatched or conflicts)
    )
    return ModelDailyReceipt(
        day=request.day,
        state=EnumDailyReceiptState.GREEN if exact else EnumDailyReceiptState.RED,
        cutoff_at=cutoff,
        closes_at=closes_at,
        table_row_ids=_order(table),
        topic_row_ids=_order(topic),
        missing_row_ids=missing,
        extra_row_ids=extra,
        mismatched_row_ids=mismatched,
        conflicting_row_ids=conflicts,
        late_rows=table_late + topic_late,
        missing=len(missing),
        extra=len(extra),
        mismatched=len(mismatched),
        verb_diff=request.verb_diff,
        exact=exact,
    )


def daily_receipt_status_row(
    receipt: ModelDailyReceipt, config: ModelDailyReceiptConfig, now: datetime
) -> str:
    """The receipt as one STATUS ledger row. Pure."""
    cells = [
        f"{now:%Y-%m-%dT%H:%M:%SZ}", "STATUS", f"lane={config.lane}",
        f"actor=script:{config.lane}", "model=none", f"day={receipt.day}",
        f"state={receipt.state.value}",
        f"table_rows={len(receipt.table_row_ids)}", f"topic_rows={len(receipt.topic_row_ids)}",
        f"missing={receipt.missing}", f"extra={receipt.extra}", f"mismatched={receipt.mismatched}",
        f"conflicts={len(receipt.conflicting_row_ids)}", f"late={receipt.late_rows}",
        f"exact={'yes' if receipt.exact else 'no'}",
        f"verb_diff={format_verb_diff(receipt.verb_diff)}",
        f"closes_at={receipt.closes_at:%Y-%m-%dT%H:%M:%SZ}",
        f"Daily table-to-topic receipt for UTC day {receipt.day}",
    ]  # fmt: skip
    return " | ".join(cells)


__all__: list[str] = [
    "build_daily_receipt",
    "daily_receipt_status_row",
    "load_daily_receipt_config",
    "receipt_window",
]
