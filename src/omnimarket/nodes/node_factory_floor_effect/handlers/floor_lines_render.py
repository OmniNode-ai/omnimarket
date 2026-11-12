# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the line board from one published record: record in, text out (the pure half of the ``lines`` operation).

No clock and no read here: the caller passes the record and ``now``. The eight source health entries print first
(a source the record does not list prints as unread, never as read); each line prints its stage counts, owner,
``ruled_by`` and stops. A record that does not carry a field says so in the contract's words instead of printing a
value that looks clean.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.nodes.node_factory_floor_effect.models.model_floor_lines import (
    ModelFloorLinesConfig,
    ModelFloorLinesRecord,
    ModelRecordHealth,
    ModelRecordLine,
    ModelRecordStop,
)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_minutes(record: ModelFloorLinesRecord, now: datetime) -> int:
    """Whole minutes from the record's time to ``now``, never negative."""
    return max(0, int((now - record.generated_at).total_seconds() // 60))


def is_stale(
    record: ModelFloorLinesRecord, now: datetime, cfg: ModelFloorLinesConfig
) -> bool:
    return age_minutes(record, now) > cfg.record_max_age_min


def _cut(text: str, cfg: ModelFloorLinesConfig) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= cfg.sentence_chars:
        return one_line
    return one_line[: cfg.sentence_chars].rstrip() + cfg.words.cut_mark


def _health_line(
    source: str, entry: ModelRecordHealth | None, cfg: ModelFloorLinesConfig
) -> str:
    if entry is None:
        return f"  {source}: unread - {cfg.words.unread_not_in_record}"
    parts = [f"  {source}: {entry.state}"]
    if entry.read_at:
        parts.append(f" {entry.read_at}")
    if entry.cause:
        parts.append(f" - {_cut(entry.cause, cfg)}")
    return "".join(parts)


def _stop_lines(stop: ModelRecordStop, cfg: ModelFloorLinesConfig) -> list[str]:
    head = f"    stop {stop.kind}: {_cut(stop.sentence, cfg)}"
    if stop.since:
        head += f" (since {stop.since})"
    lines = [head]
    cause = stop.cause or cfg.words.cause_not_recorded
    lines.append(f"      cause: {_cut(cause, cfg)}")
    if stop.evidence:
        lines.append(
            "      evidence: "
            + ", ".join(f"{item.kind} {item.ref}".strip() for item in stop.evidence)
        )
    return lines


def _line_block(line: ModelRecordLine, cfg: ModelFloorLinesConfig) -> list[str]:
    condition = line.condition or "unknown"
    title = f"  {line.line_id} [{condition}]"
    if line.name:
        title += f" {_cut(line.name, cfg)}"
    block = [title]
    if line.owner:
        block.append(f"    owner: {line.owner}")
    if line.ruled_by:
        block.append(f"    ruled by: {', '.join(line.ruled_by)}")
    if line.stage_counts is not None:
        counts = ", ".join(
            f"{stage} {line.stage_counts.get(stage, 0)}" for stage in cfg.stage_order
        )
        block.append(f"    stages: {counts}")
    if line.stops is None:
        block.append(f"    stops: {cfg.words.stops_not_in_record}")
    elif not line.stops:
        block.append(f"    stops: {cfg.words.no_stops}")
    else:
        for stop in line.stops:
            block.extend(_stop_lines(stop, cfg))
    return block


def render_board(
    record: ModelFloorLinesRecord, now: datetime, cfg: ModelFloorLinesConfig
) -> str:
    """The board text for one record. A stale record prints its UNREAD line first, then the same board."""
    age = age_minutes(record, now)
    out: list[str] = []
    if is_stale(record, now, cfg):
        out.append(
            cfg.words.unread_record.format(
                age_min=age, limit_min=cfg.record_max_age_min
            )
        )
    out.append(f"BOARD record {_stamp(record.generated_at)}, {age} min old")
    out.append("SOURCES")
    by_source = {entry.source: entry for entry in record.sources.health}
    out.extend(
        _health_line(name, by_source.get(name), cfg) for name in cfg.health_sources
    )
    out.append(f"LINES {len(record.lines)}")
    for line in record.lines:
        out.extend(_line_block(line, cfg))
    return "\n".join(out) + "\n"
