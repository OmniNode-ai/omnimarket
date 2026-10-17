# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabStoryLog: write one ISO week of the work ledger as a sanitized story log."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_lab_story_log_effect.handlers.hygiene import (
    LabStoryLogLeakError,
    load_sanitizer,
)
from omnimarket.nodes.node_lab_story_log_effect.handlers.ledger_rows import (
    merged_prs,
    read_week,
)
from omnimarket.nodes.node_lab_story_log_effect.handlers.story_render import render_log
from omnimarket.nodes.node_lab_story_log_effect.models import (
    STORY_LOG_RELATIVE_DIR,
    ModelLabStoryLogRequest,
    ModelLabStoryLogResult,
)

_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def resolve_week(week: str, now: datetime | None) -> tuple[str, datetime, datetime]:
    """The ISO week label and its [Monday 00:00Z, next Monday 00:00Z) range."""
    if week == "previous":
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        monday = (instant - timedelta(days=instant.weekday() + 7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        year, number, _ = monday.isocalendar()
    else:
        match = _WEEK.match(week)
        if match is None:
            raise ValueError(
                f"week must be 'previous' or an ISO week like 2026-W42, not {week!r}"
            )
        year, number = int(match.group(1)), int(match.group(2))
        try:
            monday = datetime.fromisocalendar(year, number, 1).replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError(f"{week!r} is not an ISO week") from exc
    return f"{year}-W{number:02d}", monday, monday + timedelta(days=7)


def log_path(output_root: Path, label: str) -> Path:
    return output_root / STORY_LOG_RELATIVE_DIR / f"{label}.md"


class HandlerLabStoryLog:
    """Read the ledger for a week, replace private names with roles, write the markdown."""

    def handle(self, request: ModelLabStoryLogRequest) -> ModelLabStoryLogResult:
        label, start, end = resolve_week(request.week, request.now)
        clean = load_sanitizer(
            request.hygiene_vocabulary_path,
            request.person_names_path,
            request.lab_vocabulary_path,
        )
        rows = read_week(request.ledger_paths, start, end)
        prs = list(merged_prs(rows))
        markdown = render_log(
            week_label=label, start=start, end=end, rows=rows, prs=prs, clean=clean
        )
        survivors = clean.leaks(markdown)
        if survivors:
            classes = sorted({name for name, _ in survivors})
            raise LabStoryLogLeakError(
                f"{len(survivors)} private name(s) survived replacement (classes: {', '.join(classes)}); "
                "nothing was written"
            )
        target = log_path(request.output_root, label)
        written = False
        if not request.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(markdown, encoding="utf-8")
            written = True
        return ModelLabStoryLogResult(
            week=label,
            range_start=start,
            range_end=end,
            output_path=target,
            written=written,
            rows_in_week=len(rows),
            last_row_stamp=rows[-1].stamp if rows else None,
            rulings=sum(1 for r in rows if r.kind == "RULING"),
            friction_rows=sum(1 for r in rows if r.kind == "FRICTION"),
            merged_prs=len(prs),
            replacements=dict(clean.counts),
            markdown=markdown,
        )


__all__ = ["HandlerLabStoryLog", "log_path", "resolve_week"]
