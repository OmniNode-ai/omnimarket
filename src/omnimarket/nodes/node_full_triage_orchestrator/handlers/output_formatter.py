# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serializers for ModelTriageReport → markdown + JSON.

Kept separate from handler logic so the output format can evolve independently
(new columns, evidence links) without touching orchestration code.
"""

from __future__ import annotations

import json

from omnibase_core.models.triage import ModelTriageReport


def report_to_json(report: ModelTriageReport) -> str:
    """Stable JSON rendering of a triage report."""
    return report.model_dump_json(indent=2)


def report_to_markdown(report: ModelTriageReport) -> str:
    """Human-readable markdown summary + ranked finding table."""
    sev = report.severity_counts
    probe_counts = report.probe_status_counts

    lines: list[str] = [
        f"# Triage report — {report.run_id}",
        "",
        f"- Started: `{report.started_at.isoformat()}`",
        f"- Completed: `{report.completed_at.isoformat()}`",
        f"- Duration: `{report.total_duration_ms}ms`",
        "",
        "## Severity summary",
        "",
        "| Severity | Count |",
        "|---|---|",
        f"| CRITICAL | {sev['CRITICAL']} |",
        f"| HIGH | {sev['HIGH']} |",
        f"| MEDIUM | {sev['MEDIUM']} |",
        f"| LOW | {sev['LOW']} |",
        f"| INFO | {sev['INFO']} |",
        "",
        "## Probe execution",
        "",
        "| Status | Count |",
        "|---|---|",
        f"| SUCCESS | {probe_counts['SUCCESS']} |",
        f"| DEGRADED | {probe_counts['DEGRADED']} |",
        f"| ERROR | {probe_counts['ERROR']} |",
        f"| TIMEOUT | {probe_counts['TIMEOUT']} |",
        f"| SKIPPED | {probe_counts['SKIPPED']} |",
        "",
        "## Ranked findings",
        "",
    ]

    if not report.ranked_findings:
        lines.append("_No findings._")
    else:
        lines.extend(
            [
                "| # | Severity | Blast | Freshness | Probe | Message |",
                "|---|---|---|---|---|---|",
            ]
        )
        for i, f in enumerate(report.ranked_findings, start=1):
            msg = f.message.replace("|", "\\|")
            lines.append(
                f"| {i} | {f.severity.value} | {f.blast_radius.value} "
                f"| {f.freshness.value} | `{f.source_probe}` | {msg} |"
            )

    lines.append("")
    lines.append("## Probe results")
    lines.append("")
    for r in report.probe_results:
        lines.append(
            f"- `{r.probe_name}` — {r.status.value} "
            f"({r.duration_ms}ms, {len(r.findings)} findings)"
            + (f" — {r.error_message}" if r.error_message else "")
        )

    return "\n".join(lines) + "\n"


def report_to_json_dict(report: ModelTriageReport) -> dict[str, object]:
    """Raw dict form for programmatic callers (tests, downstream skills)."""
    raw = json.loads(report.model_dump_json())
    return dict(raw)
