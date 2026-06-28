# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_deep_dive_report_effect (OMN-13725).

EFFECT node and the **sole owner** of git/``gh``/Linear I/O for the deep-dive
report.  All actual reads are routed through the injected
``ProtocolReportDataSource`` adapter — the handler body contains **no**
``subprocess`` / ``httpx`` / file access.  Once the adapter returns typed data
(``RepoDay`` / ``DriftReport`` from ``omnibase_infra.deep_dive``), the handler
applies the *pure* scoring + rendering helpers from the same shared module, so
there is one source of truth shared with ``generate_deep_dive.py`` (R7).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.deep_dive import (
    GitHubMergedPR,
    collect_all_ticket_ids,
    effectiveness_score_v2,
    sectionize_highlights,
    unique_commit_entries,
    velocity_score_v2,
)

from omnimarket.nodes.node_deep_dive_report_effect.adapters.adapter_local_git_report_data_source import (
    AdapterLocalGitReportDataSource,
)
from omnimarket.nodes.node_deep_dive_report_effect.models.model_deep_dive_report_io import (
    ModelDeepDiveMetrics,
    ModelDeepDiveReportCommand,
    ModelDeepDiveReportResultEvent,
)
from omnimarket.nodes.node_deep_dive_report_effect.protocols.protocol_report_data_source import (
    ProtocolReportDataSource,
)

_HANDLER_ID = "node_deep_dive_report_effect"


class HandlerDeepDiveReportEffect:
    """EFFECT: scan a workspace and emit the daily deep-dive report.

    Args:
        data_source: The injected read surface (workspace + git/``gh``). When
            omitted, the local-clones adapter is used. Injecting a mock keeps
            golden-chain tests deterministic (no real git/``gh``).
    """

    def __init__(self, data_source: ProtocolReportDataSource | None = None) -> None:
        self._data_source: ProtocolReportDataSource = (
            data_source
            if data_source is not None
            else AdapterLocalGitReportDataSource()
        )

    async def handle(
        self, command: ModelDeepDiveReportCommand
    ) -> ModelHandlerOutput[None]:
        ds = self._data_source
        date = ds.resolve_date(command.date)
        date_str = date.isoformat()

        if command.dry_run:
            event = self._quiet_event(command, date_str)
            return self._wrap(command, event)

        root = Path(command.root)
        repos = ds.discover_repos(root, command.repo_prefixes)
        repo_days = [
            rd
            for repo in repos
            if (rd := ds.scan_repo_day(repo, date, include_dirty=command.include_dirty))
            is not None
        ]
        drift = ds.compute_drift(repo_days, root, date)

        all_prs: list[tuple[str, GitHubMergedPR]] = [
            (rd.name, pr) for rd in repo_days for pr in rd.github_merged_prs
        ]
        category_counts: dict[str, int] = dict(
            Counter(pr.category for _, pr in all_prs)
        )
        unique_repos_with_merges = len({name for name, _ in all_prs})

        eff_score, eff_expl = effectiveness_score_v2(category_counts, drift.penalty)
        vel_score, vel_expl = velocity_score_v2(
            all_prs, unique_repos_with_merges, drift.penalty
        )
        uniq_commits = unique_commit_entries(repo_days)
        ticket_ids = tuple(collect_all_ticket_ids(repo_days))
        total_commits = sum(len(rd.commits) for rd in repo_days)
        quiet_day = total_commits == 0 and not all_prs

        metrics = ModelDeepDiveMetrics(
            effectiveness_score=eff_score,
            velocity_score=vel_score,
            drift_level=drift.level,
            drift_penalty=drift.penalty,
            total_prs=len(all_prs),
            unlinked_pr_count=drift.unlinked_pr_count,
            category_counts=category_counts,
            unique_commits=len(uniq_commits),
            repos_active=len(repo_days),
            ticket_ids=ticket_ids,
        )

        markdown = self._render(
            date_str=date_str,
            metrics=metrics,
            eff_expl=eff_expl,
            vel_expl=vel_expl,
            drift_risks=drift.risks,
            highlights=sectionize_highlights(uniq_commits),
            ticket_ids=ticket_ids,
            quiet_day=quiet_day,
            total_commits=total_commits,
        )
        event = ModelDeepDiveReportResultEvent(
            correlation_id=command.correlation_id,
            report_date=date_str,
            report_markdown=markdown,
            metrics=metrics,
            quiet_day=quiet_day,
        )
        return self._wrap(command, event)

    # ------------------------------------------------------------------
    # Pure rendering + envelope helpers (no I/O)
    # ------------------------------------------------------------------

    def _quiet_event(
        self, command: ModelDeepDiveReportCommand, date_str: str
    ) -> ModelDeepDiveReportResultEvent:
        metrics = ModelDeepDiveMetrics(
            effectiveness_score=0,
            velocity_score=0,
            drift_level="green",
            drift_penalty=0,
            total_prs=0,
            unlinked_pr_count=0,
            category_counts={},
            unique_commits=0,
            repos_active=0,
            ticket_ids=(),
        )
        return ModelDeepDiveReportResultEvent(
            correlation_id=command.correlation_id,
            report_date=date_str,
            report_markdown=f"# Deep Dive — {date_str}\n\nQuiet day — 0 commits "
            "(dry_run: no workspace reads performed).\n",
            metrics=metrics,
            quiet_day=True,
        )

    @staticmethod
    def _render(
        *,
        date_str: str,
        metrics: ModelDeepDiveMetrics,
        eff_expl: str,
        vel_expl: str,
        drift_risks: list[tuple[str, str]],
        highlights: dict[str, list[str]],
        ticket_ids: tuple[str, ...],
        quiet_day: bool,
        total_commits: int,
    ) -> str:
        lines: list[str] = [f"# Deep Dive — {date_str}", ""]
        if quiet_day:
            lines.append(f"Quiet day — {total_commits} commits, 0 merged PRs.")
            lines.append("")
        lines.append("## Scorecard")
        lines.append(f"- Effectiveness: **{metrics.effectiveness_score}** ({eff_expl})")
        lines.append(f"- Velocity: **{metrics.velocity_score}** ({vel_expl})")
        lines.append(
            f"- Drift: **{metrics.drift_level}** "
            f"(penalty {metrics.drift_penalty}, "
            f"{metrics.unlinked_pr_count} unlinked of {metrics.total_prs} PRs)"
        )
        lines.append(
            f"- Active repos: {metrics.repos_active} | "
            f"unique commits: {metrics.unique_commits}"
        )
        if metrics.category_counts:
            cats = ", ".join(
                f"{k}: {v}" for k, v in sorted(metrics.category_counts.items())
            )
            lines += ["", "## PR Categories", cats]
        if drift_risks:
            lines += ["", "## Drift Risks"]
            lines += [f"- {name}: {reason}" for name, reason in drift_risks]
        if highlights:
            lines += ["", "## Major Components & Work Completed"]
            for section, items in highlights.items():
                lines.append(f"### {section}")
                lines += [f"- {item}" for item in items]
        if ticket_ids:
            lines += ["", "## Tickets", ", ".join(ticket_ids)]
        return "\n".join(lines) + "\n"

    def _wrap(
        self,
        command: ModelDeepDiveReportCommand,
        event: ModelDeepDiveReportResultEvent,
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(event,),
        )
