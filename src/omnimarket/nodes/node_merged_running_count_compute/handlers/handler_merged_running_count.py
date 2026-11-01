# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged, merged-and-running and merged-not-running per operator day, and idle slots per host.

Pure: the caller reads the merges, the running-on-dev evidence, the host table with its placement read and the live
lanes, and supplies them with the clock. Every vocabulary, window and timezone is in ``request.config``, which
the reader effect loads from this node's ``contract.yaml`` or from an overlay.
A hand-off, an armed PR and a queued PR are pull request states the config does not count, so no path here can
raise a count from one.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMergedRunningCountRequest,
    ModelMergedRunningCountResult,
    ModelMrcConfig,
    ModelMrcDay,
    ModelMrcEvidence,
    ModelMrcHostIdle,
    ModelMrcMergeVerdict,
    ModelMrcPr,
    ModelMrcRepoCount,
)


def _bare_repo(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


class HandlerMergedRunningCount:
    """handle(request) -> result: the merged-and-running count and the idle slots."""

    def handle(
        self, request: ModelMergedRunningCountRequest
    ) -> ModelMergedRunningCountResult:
        config = request.config
        zone = ZoneInfo(config.operator_timezone)
        known_kinds = {source.kind for source in config.evidence_sources}
        usable = [e for e in request.evidence if e.kind in known_kinds]
        by_number: dict[tuple[str, int], ModelMrcEvidence] = {}
        by_sha: dict[tuple[str, str], ModelMrcEvidence] = {}
        for item in sorted(usable, key=lambda e: e.observed_at):
            repo = _bare_repo(item.repo)
            if item.number is not None:
                by_number.setdefault((repo, item.number), item)
            if item.merge_sha:
                by_sha.setdefault(
                    (repo, item.merge_sha[: config.sha_match_chars]), item
                )

        verdicts = [
            self._verdict(pr, merged_at, config, zone, by_number, by_sha)
            for pr, merged_at in self._counted(request.prs, config, request.now)
        ]
        today = request.now.astimezone(zone).date()
        days = tuple(
            self._day(today - timedelta(days=back), zone, verdicts)
            for back in range(config.window_days)
        )
        idle_hosts, unlisted = self._idle(request)
        return ModelMergedRunningCountResult(
            schema_id=config.schema_id,
            generated_at=request.now,
            operator_timezone=config.operator_timezone,
            window_days=config.window_days,
            days=days,
            idle_hosts=idle_hosts,
            idle_total=sum(h.idle or 0 for h in idle_hosts),
            lanes_on_unlisted_hosts=unlisted,
            ignored_evidence=len(request.evidence) - len(usable),
        )

    @staticmethod
    def _counted(
        prs: tuple[ModelMrcPr, ...], config: ModelMrcConfig, now: datetime
    ) -> list[tuple[ModelMrcPr, datetime]]:
        seen: dict[tuple[str, int], tuple[ModelMrcPr, datetime]] = {}
        for pr in prs:
            if (
                pr.state in config.counted_pr_states
                and pr.merged_at is not None
                and pr.merged_at <= now
                and pr.base_branch in config.landing_branches
            ):
                seen.setdefault((_bare_repo(pr.repo), pr.number), (pr, pr.merged_at))
        return sorted(
            seen.values(),
            key=lambda item: (item[1], _bare_repo(item[0].repo), item[0].number),
        )

    @staticmethod
    def _verdict(
        pr: ModelMrcPr,
        merged_at: datetime,
        config: ModelMrcConfig,
        zone: ZoneInfo,
        by_number: dict[tuple[str, int], ModelMrcEvidence],
        by_sha: dict[tuple[str, str], ModelMrcEvidence],
    ) -> ModelMrcMergeVerdict:
        repo = _bare_repo(pr.repo)
        evidence = by_number.get((repo, pr.number))
        if evidence is None and pr.merge_sha:
            evidence = by_sha.get((repo, pr.merge_sha[: config.sha_match_chars]))
        reason = ""
        if evidence is None:
            reason = config.no_evidence_source_reason
            for source in config.evidence_sources:
                if repo in source.repos:
                    reason = source.missing_reason
                    break
        return ModelMrcMergeVerdict(
            repo=repo,
            number=pr.number,
            merge_sha=pr.merge_sha,
            merged_at=merged_at,
            utc_date=merged_at.astimezone(ZoneInfo("UTC")).date().isoformat(),
            local_date=merged_at.astimezone(zone).date().isoformat(),
            running=evidence is not None,
            evidence_kind=evidence.kind if evidence else "",
            evidence_source=evidence.source if evidence else "",
            reason=reason,
        )

    @staticmethod
    def _day(
        day: date, zone: ZoneInfo, verdicts: list[ModelMrcMergeVerdict]
    ) -> ModelMrcDay:
        local_date = day.isoformat()
        start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), tzinfo=zone
        )
        merges = tuple(v for v in verdicts if v.local_date == local_date)
        per_repo: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for v in merges:
            per_repo[v.repo][0] += 1
            per_repo[v.repo][1] += 1 if v.running else 0
        running = sum(1 for v in merges if v.running)
        return ModelMrcDay(
            local_date=local_date,
            utc_start=start.astimezone(ZoneInfo("UTC")),
            utc_end=end.astimezone(ZoneInfo("UTC")),
            merged=len(merges),
            running=running,
            not_running=len(merges) - running,
            by_repo=tuple(
                ModelMrcRepoCount(repo=repo, merged=m, running=r, not_running=m - r)
                for repo, (m, r) in sorted(per_repo.items())
            ),
            merges=merges,
        )

    @staticmethod
    def _idle(
        request: ModelMergedRunningCountRequest,
    ) -> tuple[tuple[ModelMrcHostIdle, ...], tuple[str, ...]]:
        live: dict[str, int] = defaultdict(int)
        for lane in request.live_lanes:
            live[lane.host] += lane.slots
        listed = {host.host for host in request.hosts}
        unlisted = tuple(
            sorted(lane.lane for lane in request.live_lanes if lane.host not in listed)
        )
        rows: list[ModelMrcHostIdle] = []
        for host in sorted(request.hosts, key=lambda h: h.host):
            held = live.get(host.host, 0)
            if not host.available:
                rows.append(
                    ModelMrcHostIdle(
                        host=host.host,
                        state="out",
                        slots=host.slots,
                        live_slots=held,
                        idle=0,
                    )
                )
            elif host.slots is None:
                rows.append(
                    ModelMrcHostIdle(
                        host=host.host,
                        state="unread",
                        slots=None,
                        live_slots=held,
                        idle=None,
                    )
                )
            else:
                rows.append(
                    ModelMrcHostIdle(
                        host=host.host,
                        state="read",
                        slots=host.slots,
                        live_slots=held,
                        idle=max(0, host.slots - held),
                    )
                )
        return tuple(rows), unlisted
