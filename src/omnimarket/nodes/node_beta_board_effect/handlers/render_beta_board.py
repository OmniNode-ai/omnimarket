# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The beta board page, drawn on the server side so a plain fetch of the page already carries every criterion (OMN-23315)."""

from __future__ import annotations

from datetime import datetime
from html import escape

from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoard,
    ModelCriterionState,
)

_LABEL = {
    "pass": "PASS",
    "fail": "FAIL",
    "stale": "STALE",
    "unread": "UNREAD",
    "no_reading": "NO READING",
}

_CSS = """:root{--ink:#1c2024;--mute:#646b73;--line:#dfe2e6;--bg:#f6f7f8;--card:#fff;--red:#b3261e;--amber:#a56a00;--green:#1f7a4d;--grey:#9aa1a9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px}
.sub{color:var(--mute);margin:0 0 14px}.card{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:hidden}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600}tr:last-child td{border-bottom:0}
.st{font-weight:700;white-space:nowrap}.st-pass{color:var(--green)}.st-fail{color:var(--red)}.st-stale,.st-unread{color:var(--amber)}.st-no_reading{color:var(--grey)}
.mute{color:var(--mute)}.cond{max-width:420px}.id{font-weight:700;white-space:nowrap}code{font-size:12px}
a{color:#2b6cb0}nav a{margin-right:12px}"""


def _clock(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%SZ") if moment else "-"


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _row(c: ModelCriterionState) -> str:
    since = _clock(c.since)
    if c.since is not None and c.since_is_lower_bound:
        since = f"at least {since}"
    carriers = ", ".join(escape(t) for t in c.carriers) or "-"
    reads = "; ".join(
        f"{escape(s.source_id)}: {escape(s.state)}"
        + (f" ({escape(s.detail)})" if s.detail else "")
        for s in c.sources
    )
    return (
        f'<tr data-criterion="{escape(c.id)}" data-state="{escape(c.state)}">'
        f'<td class="id">{escape(c.id)}</td>'
        f'<td class="cond">{escape(c.condition)}<div class="mute">{escape(c.proof)}</div></td>'
        f'<td class="st st-{escape(c.state)}">{_LABEL[c.state]}</td>'
        f"<td>{escape(since)}</td>"
        f'<td data-age-seconds="{"" if c.reading_age_seconds is None else c.reading_age_seconds}">{_age(c.reading_age_seconds)}</td>'
        f'<td>{escape(c.owner_ticket)}<div class="mute">carriers: {carriers}</div></td>'
        f'<td class="mute">{escape(c.reason)}<div>{reads}</div></td></tr>'
    )


def render_page(board: ModelBetaBoard, refresh_seconds: int) -> str:
    """The whole page: the milestone line, one row per criterion, and the probes still read from a cloud repository."""
    counts = ", ".join(f"{_LABEL[k].lower()} {v}" for k, v in board.counts.items() if v)
    verdict = "met" if board.met else "not met"
    rows = "\n".join(_row(c) for c in board.criteria)
    if board.cloud_probes:
        probe_rows = "\n".join(
            f'<tr data-probe="{escape(p.probe_id)}"><td class="id">{escape(p.probe_id)}</td>'
            f"<td>{escape(p.repository)}</td><td><code>{escape(p.workflow)}</code></td>"
            f"<td>{escape(', '.join(p.criteria)) or '-'}</td>"
            f'<td class="st st-{escape(p.state)}">{_LABEL[p.state]}</td></tr>'
            for p in board.cloud_probes
        )
        cloud = (
            f"<h2>Probes still read from a cloud repository ({len(board.cloud_probes)})</h2>"
            '<p class="sub">These workflows have not moved yet; the board reads each one where it is and adds nothing to that repository.</p>'
            '<div class="card"><table><thead><tr><th>Probe</th><th>Repository</th><th>Workflow</th><th>Criteria</th><th>State</th></tr></thead>'
            f"<tbody>{probe_rows}</tbody></table></div>"
        )
    else:
        cloud = '<h2>Probes still read from a cloud repository (0)</h2><p class="sub">None: every probe is read where it runs.</p>'
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
        f"<title>Beta board {escape(board.milestone_id)}</title>"
        '<link rel="icon" type="image/svg+xml" href="favicon.svg">'
        f"<style>{_CSS}</style></head><body><main>"
        '<nav><a href="index.html">Factory floor</a><a href="detail.html">Detail</a><a href="delegation.html">Delegation</a></nav>'
        f"<h1>Beta board: {escape(board.milestone_id)} {escape(board.milestone_title)}</h1>"
        f'<p class="sub" id="verdict">Milestone {verdict}: {counts}. Computed {_clock(board.generated_at)}; '
        'the data is <a href="beta-board.json">beta-board.json</a>.</p>'
        '<div class="card"><table><thead><tr><th>Criterion</th><th>Condition and proof</th><th>State</th><th>Since</th>'
        "<th>Reading age</th><th>Owner ticket</th><th>Readings</th></tr></thead>"
        f"<tbody>\n{rows}\n</tbody></table></div>"
        f"{cloud}</main></body></html>\n"
    )
