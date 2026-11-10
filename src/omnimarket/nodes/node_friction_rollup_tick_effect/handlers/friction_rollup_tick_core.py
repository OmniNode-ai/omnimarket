# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure decisions of the friction rollup tick, ported from the friction rollup timer (OMN-22987).

No file is read, no clock is read and nothing is written here. Every value the timer held as a module constant
(the windows, the top-N, the budgets, the schema, the lane, the ticket) is taken from the settings.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
    UNCLASSIFIED,
    render_markdown,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickSettings,
)

STAMPED_REPORT = re.compile(r"^(\d{8}T\d{6}Z)\.(?:json|md)$")


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def report_directory_reference(
    state_dir: Path, registry_root: Path, settings: ModelFrictionRollupTickSettings
) -> str:
    """How the STATUS row names the report directory: relative to the registry root when it is under it, else as given."""
    try:
        relative = state_dir.relative_to(registry_root).as_posix()
    except ValueError:
        return state_dir.as_posix()
    return relative if relative != "." else settings.state_subdir


def trend_key(window_name: str) -> str:
    """The trend field that holds the deltas of the window with this configured name."""
    return f"totals_{window_name}_delta"


def _cell(value: str) -> str:
    """A value safe inside one ledger cell: no pipe, whitespace or comma."""
    cleaned = "".join(
        "-" if ch == "|" or ch == "," or ch.isspace() else ch for ch in value
    )
    return cleaned or "-"


def _signed(value: float) -> str:
    number = round(value)
    return f"+{number}" if number >= 0 else str(number)


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _classes(report: dict[str, object]) -> list[dict[str, Any]]:
    return [
        item
        for item in _as_list(report.get("classes"))
        if isinstance(item, dict) and item.get("class") != UNCLASSIFIED
    ]


def top_classes(report: dict[str, object], limit: int) -> list[dict[str, object]]:
    """The costliest named classes, unclassified rows excluded."""
    ranked = sorted(
        _classes(report),
        key=lambda item: (
            -_number(item.get("cost_minutes")),
            -_number(item.get("occurrences")),
            str(item.get("class")),
        ),
    )
    return [
        {
            "class": str(item.get("class")),
            "cost_minutes": _number(item.get("cost_minutes")),
            "occurrences": int(_number(item.get("occurrences"))),
            "tickets": [str(ticket) for ticket in _as_list(item.get("tickets"))],
        }
        for item in ranked[:limit]
    ]


def needs_ticket(report: dict[str, object]) -> list[dict[str, object]]:
    """Every named class seen at least twice that cites no owning ticket."""
    flagged = [
        item
        for item in _classes(report)
        if item.get("recurring_without_ticket") is True
    ]
    flagged.sort(
        key=lambda item: (-_number(item.get("cost_minutes")), str(item.get("class")))
    )
    return [
        {
            "class": str(item.get("class")),
            "occurrences": int(_number(item.get("occurrences"))),
            "cost_minutes": _number(item.get("cost_minutes")),
            "lanes": [str(lane) for lane in _as_list(item.get("lanes"))],
        }
        for item in flagged
    ]


def previous_document(
    payload: object, settings: ModelFrictionRollupTickSettings
) -> dict[str, Any] | None:
    """The previous tick's document, or None when it is absent, unreadable or foreign."""
    if not isinstance(payload, dict) or payload.get("schema") != settings.schema_name:
        return None
    windows = payload.get("windows")
    if not isinstance(windows, dict) or not all(
        isinstance(windows.get(window.name), dict) for window in settings.windows
    ):
        return None
    return payload


def _totals(report: dict[str, object]) -> dict[str, Any]:
    return _as_dict(report.get("totals"))


def compute_trend(
    current: dict[str, dict[str, object]],
    top: list[dict[str, object]],
    flagged: list[dict[str, object]],
    previous: dict[str, Any] | None,
    previous_path: str,
    settings: ModelFrictionRollupTickSettings,
) -> dict[str, object]:
    """Deltas against the previous tick; ``previous`` is None on a first run."""
    if previous is None:
        return {"previous": None}
    primary, short = settings.primary.name, settings.short.name
    prev_windows = _as_dict(previous.get("windows"))
    prev_primary = _as_dict(prev_windows.get(primary))
    prev_short = _as_dict(prev_windows.get(short))
    cur_primary, cur_short = _totals(current[primary]), _totals(current[short])
    old_primary, old_short = _totals(prev_primary), _totals(prev_short)
    prev_classes = {str(item.get("class")): item for item in _classes(prev_primary)}
    current_names = [str(item.get("class")) for item in _classes(current[primary])]
    prev_flagged = {
        str(item.get("class"))
        for item in _as_list(_as_dict(previous.get("summary")).get("needs_ticket"))
        if isinstance(item, dict)
    }
    now_flagged = {str(item["class"]) for item in flagged}
    top_deltas: list[dict[str, object]] = []
    for item in top:
        name = str(item["class"])
        before = prev_classes.get(name)
        top_deltas.append(
            {
                "class": name,
                "cost_minutes_delta": (
                    None
                    if before is None
                    else round(
                        _number(item["cost_minutes"])
                        - _number(before.get("cost_minutes")),
                        6,
                    )
                ),
            }
        )
    return {
        "previous": {
            "generated_at": previous.get("generated_at"),
            "path": previous_path,
        },
        trend_key(primary): {
            key: round(_number(cur_primary.get(key)) - _number(old_primary.get(key)), 6)
            for key in ("friction_rows", "occurrences", "cost_minutes")
        },
        trend_key(short): {
            key: round(_number(cur_short.get(key)) - _number(old_short.get(key)), 6)
            for key in ("friction_rows", "cost_minutes")
        },
        "top_classes_delta": top_deltas,
        "new_classes": sorted(
            name for name in current_names if name not in prev_classes
        ),
        "needs_ticket_added": sorted(now_flagged - prev_flagged),
        "needs_ticket_cleared": sorted(prev_flagged - now_flagged),
    }


def _limited(items: list[str], separator: str, budget: int) -> str:
    """Join items, dropping the tail with ``+<k> more`` when over budget."""
    kept: list[str] = []
    for item in items:
        if kept and len(separator.join([*kept, item])) > budget:
            return separator.join(kept) + f"{separator}+{len(items) - len(kept)} more"
        kept.append(item)
    return separator.join(kept)


def status_row(
    now: datetime,
    current: dict[str, dict[str, object]],
    summary: dict[str, Any],
    report_name: str,
    settings: ModelFrictionRollupTickSettings,
    class_rules_note: str | None = None,
    report_dir: str | None = None,
) -> str:
    """The one STATUS row a tick appends. It must pass the ledger lock's lints.

    ``report_dir`` names where the reports are; it defaults to the settings' relative ``state_subdir``."""
    primary, short = settings.primary, settings.short
    window = _as_dict(current[primary.name].get("window"))
    t_primary, t_short = _totals(current[primary.name]), _totals(current[short.name])
    top_items = [
        f"{_cell(str(item['class']))} {round(_number(item['cost_minutes']))}min"
        f" x{item['occurrences']}"
        f" [{'+'.join(_cell(str(t)) for t in _as_list(item['tickets'])) or 'none'}]"
        for item in _as_list(summary["top_classes"])
    ]
    top_cell = (
        _limited(top_items, "; ", settings.top_budget_chars) if top_items else "none"
    )
    trend = _as_dict(summary["trend"])
    previous = trend.get("previous")
    if previous is None:
        trend_cell = "first run"
    else:
        d24 = _as_dict(trend.get(trend_key(primary.name)))
        d2 = _as_dict(trend.get(trend_key(short.name)))
        new = [_cell(str(name)) for name in _as_list(trend.get("new_classes"))]
        new_text = (
            _limited(new, " ", settings.new_classes_budget_chars) if new else "none"
        )
        trend_cell = (
            f"vs {_cell(str(_as_dict(previous).get('generated_at')))}:"
            f" rows {_signed(_number(d24.get('friction_rows')))},"
            f" occurrences {_signed(_number(d24.get('occurrences')))},"
            f" cost {_signed(_number(d24.get('cost_minutes')))} min"
            f" ({short.name} cost {_signed(_number(d2.get('cost_minutes')))} min);"
            f" new classes: {new_text}"
        )
    flagged = _as_list(summary["needs_ticket"])
    if flagged:
        flagged_items = [
            f"{_cell(str(item['class']))}(x{item['occurrences']}"
            f" {round(_number(item['cost_minutes']))}min)"
            for item in flagged
        ]
        needs_cell = f"flag=needs-ticket classes={_limited(flagged_items, '; ', settings.needs_ticket_budget_chars)}"
    else:
        needs_cell = "needs-ticket=none"
    cells = [
        stamp(now),
        "STATUS",
        f"lane={settings.lane}",
        f"actor={settings.actor}",
        f"model={settings.model}",
        f"ticket={settings.ticket}",
        f"related={settings.umbrella_ticket}",
        f"window={primary.name} since={window.get('since')} until={window.get('until')}",
        (
            f"totals{primary.hours}=rows {int(_number(t_primary.get('friction_rows')))},"
            f" occurrences {int(_number(t_primary.get('occurrences')))},"
            f" cost {_number(t_primary.get('cost_minutes')) / 60:.1f} lane-hours"
            f" ({short.name} window: rows {int(_number(t_short.get('friction_rows')))},"
            f" cost {round(_number(t_short.get('cost_minutes')))} min)"
        ),
        f"top{settings.top_n}={top_cell}",
        f"trend={trend_cell}",
        needs_cell,
    ]
    if class_rules_note is not None:
        cells.append(class_rules_note)
    directory = settings.state_subdir if report_dir is None else report_dir
    cells.append(
        f"report={directory}/{report_name} json={directory}/{settings.latest_stem}.json"
    )
    row = " | ".join(cells)
    limit = settings.max_row_chars
    return row if len(row) <= limit else row[: limit - 3] + "..."


def _joined(value: object) -> str:
    return ", ".join(str(item) for item in _as_list(value)) or "none"


def render_summary_markdown(
    now: datetime,
    current: dict[str, dict[str, object]],
    summary: dict[str, Any],
    settings: ModelFrictionRollupTickSettings,
) -> str:
    """The header of the markdown report, followed by both full rollups."""
    primary, short = settings.primary.name, settings.short.name
    lines = [
        f"# Friction rollup {stamp(now)}",
        "",
        f"## Top {settings.top_n} classes by cost (last {primary})",
        "",
    ]
    top = _as_list(summary["top_classes"])
    if top:
        lines += [
            "| class | cost (min) | occurrences | tickets |",
            "| -- | --: | --: | -- |",
        ]
        lines += [
            f"| {item['class']} | {round(_number(item['cost_minutes']))} | {item['occurrences']}"
            f" | {', '.join(_as_list(item['tickets'])) or 'none'} |"
            for item in top
        ]
    else:
        lines.append("No named class in the window.")
    lines += ["", "## Trend", ""]
    trend = _as_dict(summary["trend"])
    if trend.get("previous") is None:
        lines.append("First run: no previous tick to compare with.")
    else:
        previous = _as_dict(trend.get("previous"))
        d24 = _as_dict(trend.get(trend_key(primary)))
        d2 = _as_dict(trend.get(trend_key(short)))
        lines += [
            f"- Previous tick: {previous.get('generated_at')} ({previous.get('path')})",
            f"- {primary}: rows {_signed(_number(d24.get('friction_rows')))},"
            f" occurrences {_signed(_number(d24.get('occurrences')))},"
            f" cost {_signed(_number(d24.get('cost_minutes')))} min",
            f"- {short}: rows {_signed(_number(d2.get('friction_rows')))},"
            f" cost {_signed(_number(d2.get('cost_minutes')))} min",
            f"- New classes: {_joined(trend.get('new_classes'))}",
            f"- Newly flagged: {_joined(trend.get('needs_ticket_added'))}",
            f"- No longer flagged: {_joined(trend.get('needs_ticket_cleared'))}",
        ]
    lines += ["", "## Recurring classes with no owning ticket", ""]
    flagged = _as_list(summary["needs_ticket"])
    if flagged:
        lines += [
            f"- `flag=needs-ticket` {item['class']}: {item['occurrences']} occurrences,"
            f" {round(_number(item['cost_minutes']))} min, lanes {', '.join(item['lanes']) or '-'}"
            for item in flagged
        ]
    else:
        lines.append("None.")
    lines += [
        "",
        f"## Last {primary}",
        "",
        render_markdown(current[primary]),
        "",
        f"## Last {short}",
        "",
        render_markdown(current[short]),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def log_line(
    now: datetime,
    state: str,
    report: str,
    current: dict[str, dict[str, object]],
    top: list[dict[str, object]],
    flagged: list[dict[str, object]],
    settings: ModelFrictionRollupTickSettings,
    class_rules_note: str | None = None,
) -> str:
    t_primary = _totals(current[settings.primary.name])
    t_short = _totals(current[settings.short.name])
    first = str(top[0]["class"]) if top else "none"
    line = (
        f"friction-rollup {stamp(now)} state={state}"
        f" rows{settings.primary.hours}={int(_number(t_primary.get('friction_rows')))}"
        f" cost{settings.primary.hours}_min={round(_number(t_primary.get('cost_minutes')))}"
        f" rows{settings.short.hours}={int(_number(t_short.get('friction_rows')))}"
        f" needs_ticket={len(flagged)} top={_cell(first)} report={report}"
    )
    return line if class_rules_note is None else f"{line} {class_rules_note}"
