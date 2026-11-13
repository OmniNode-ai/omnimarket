# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Running-revision reading: compare each repository's running revision with its merged head.

Pure: the clock is ``facts.observed_at``, every bound is a policy (overlay)
value, nothing is read or sent. A reading that decides nothing: no gate, stop
or admission.
"""

from __future__ import annotations

from omnimarket.models.running_revision import (
    EnumRunningRevisionRedReason,
    EnumRunningRevisionRefusalReason,
    EnumRunningRevisionStatus,
    EnumRunningRevisionUnknownReason,
    ModelMergeToRunning,
    ModelOldestNotRunning,
    ModelRepositoryMergeHistory,
    ModelRepositoryReading,
    ModelRunningBuild,
    ModelRunningRevisionFacts,
    ModelRunningRevisionPolicy,
    ModelRunningRevisionReading,
    ModelRunningRevisionRefused,
)
from omnimarket.nodes.node_dev_running_revision_compute.overlay import (
    load_dev_running_revision_policy,
)

Result = ModelRunningRevisionReading | ModelRunningRevisionRefused


def _current_build(builds: tuple[ModelRunningBuild, ...]) -> ModelRunningBuild | None:
    """The build with the greatest running_since, or None when there are no builds."""
    if not builds:
        return None
    return max(builds, key=lambda b: b.running_since)


def read_running_revision(
    facts: ModelRunningRevisionFacts, policy: ModelRunningRevisionPolicy
) -> Result:
    """Read one tick. Deterministic in any input order."""
    now = facts.observed_at

    # 1. Refuse when the facts contradict themselves.
    seen_repos: set[str] = set()
    for history in facts.histories:
        if history.repository in seen_repos:
            return ModelRunningRevisionRefused(
                correlation_id=facts.correlation_id,
                reason=EnumRunningRevisionRefusalReason.DUPLICATE_REPOSITORY,
                detail=f"repository {history.repository} appears in more than one history",
            )
        seen_repos.add(history.repository)
        if history.repository not in policy.known_repositories:
            return ModelRunningRevisionRefused(
                correlation_id=facts.correlation_id,
                reason=EnumRunningRevisionRefusalReason.UNKNOWN_REPOSITORY,
                detail=f"history names unknown repository {history.repository}",
            )
        for merge in history.merges:
            if merge.merged_at > now:
                return ModelRunningRevisionRefused(
                    correlation_id=facts.correlation_id,
                    reason=EnumRunningRevisionRefusalReason.TIME_AFTER_OBSERVED_AT,
                    detail=f"merge {merge.sha} merged_at is after observed_at",
                )
    for build in facts.builds:
        if build.running_since > now:
            return ModelRunningRevisionRefused(
                correlation_id=facts.correlation_id,
                reason=EnumRunningRevisionRefusalReason.TIME_AFTER_OBSERVED_AT,
                detail=f"build {build.manifest.build_id} running_since is after observed_at",
            )
        if build.manifest.built_at > now:
            return ModelRunningRevisionRefused(
                correlation_id=facts.correlation_id,
                reason=EnumRunningRevisionRefusalReason.TIME_AFTER_OBSERVED_AT,
                detail=f"build {build.manifest.build_id} built_at is after observed_at",
            )

    # 2. The current build is the one with the greatest running_since.
    current = _current_build(facts.builds)

    # Build a per-repository history lookup.
    history_by_repo: dict[str, ModelRepositoryMergeHistory] = {
        h.repository: h for h in facts.histories
    }

    # 3. Per-repository readings.
    readings: list[ModelRepositoryReading] = []
    for repo in sorted(policy.known_repositories):
        found = history_by_repo.get(repo)
        if found is None:
            readings.append(
                ModelRepositoryReading(
                    repository=repo,
                    status=EnumRunningRevisionStatus.UNKNOWN,
                    running_sha=None,
                    tip_sha=None,
                    behind_by_changes=0,
                    behind_for_seconds=0,
                    oldest_not_running=None,
                    unknown_reason=EnumRunningRevisionUnknownReason.HISTORY_MISSING,
                )
            )
            continue
        history = found
        if current is None:
            readings.append(
                ModelRepositoryReading(
                    repository=repo,
                    status=EnumRunningRevisionStatus.UNKNOWN,
                    running_sha=None,
                    tip_sha=history.tip_sha,
                    behind_by_changes=0,
                    behind_for_seconds=0,
                    oldest_not_running=None,
                    unknown_reason=EnumRunningRevisionUnknownReason.NO_RUNNING_BUILD,
                )
            )
            continue
        # Find the running sha for this repository in the current manifest.
        running_sha: str | None = None
        for revision in current.manifest.revisions:
            if revision.repository == repo:
                running_sha = revision.sha
                break
        if running_sha is None:
            readings.append(
                ModelRepositoryReading(
                    repository=repo,
                    status=EnumRunningRevisionStatus.UNKNOWN,
                    running_sha=None,
                    tip_sha=history.tip_sha,
                    behind_by_changes=0,
                    behind_for_seconds=0,
                    oldest_not_running=None,
                    unknown_reason=EnumRunningRevisionUnknownReason.REVISION_MISSING_FROM_MANIFEST,
                )
            )
            continue
        # Find the index of the running sha in the merges.
        merge_shas = [m.sha for m in history.merges]
        if running_sha == history.base_sha:
            index = -1
        elif running_sha in merge_shas:
            index = merge_shas.index(running_sha)
        else:
            readings.append(
                ModelRepositoryReading(
                    repository=repo,
                    status=EnumRunningRevisionStatus.UNKNOWN,
                    running_sha=running_sha,
                    tip_sha=history.tip_sha,
                    behind_by_changes=0,
                    behind_for_seconds=0,
                    oldest_not_running=None,
                    unknown_reason=EnumRunningRevisionUnknownReason.RUNNING_REVISION_NOT_IN_HISTORY,
                )
            )
            continue
        uncontained = history.merges[index + 1 :]
        if not uncontained:
            readings.append(
                ModelRepositoryReading(
                    repository=repo,
                    status=EnumRunningRevisionStatus.CURRENT,
                    running_sha=running_sha,
                    tip_sha=history.tip_sha,
                    behind_by_changes=0,
                    behind_for_seconds=0,
                    oldest_not_running=None,
                    unknown_reason=None,
                )
            )
            continue
        behind_by_changes = len(uncontained)
        behind_for_seconds = int((now - uncontained[0].merged_at).total_seconds())
        oldest_not_running = uncontained[0]
        status = (
            EnumRunningRevisionStatus.BEHIND
            if behind_for_seconds > policy.behind_threshold_seconds
            else EnumRunningRevisionStatus.LAGGING
        )
        readings.append(
            ModelRepositoryReading(
                repository=repo,
                status=status,
                running_sha=running_sha,
                tip_sha=history.tip_sha,
                behind_by_changes=behind_by_changes,
                behind_for_seconds=behind_for_seconds,
                oldest_not_running=oldest_not_running,
                unknown_reason=None,
            )
        )

    # 4. merge_times: for every merge of every history (known repositories only).
    merge_times: list[ModelMergeToRunning] = []
    for history in facts.histories:
        if history.repository not in policy.known_repositories:
            continue
        merge_shas = [m.sha for m in history.merges]
        for i, merge in enumerate(history.merges):
            # Find the build with the smallest running_since whose manifest
            # revision for this repository is a sha at index >= i.
            contained_build: ModelRunningBuild | None = None
            for build in sorted(facts.builds, key=lambda b: b.running_since):
                build_sha: str | None = None
                for revision in build.manifest.revisions:
                    if revision.repository == history.repository:
                        build_sha = revision.sha
                        break
                if build_sha is None:
                    continue
                if build_sha not in merge_shas:
                    continue
                build_index = merge_shas.index(build_sha)
                if build_index >= i:
                    contained_build = build
                    break
            if contained_build is not None:
                seconds = max(
                    0,
                    int(
                        (
                            contained_build.running_since - merge.merged_at
                        ).total_seconds()
                    ),
                )
                merge_times.append(
                    ModelMergeToRunning(
                        repository=history.repository,
                        sha=merge.sha,
                        merged_at=merge.merged_at,
                        running_since=contained_build.running_since,
                        seconds=seconds,
                        contained=True,
                    )
                )
            else:
                seconds = int((now - merge.merged_at).total_seconds())
                merge_times.append(
                    ModelMergeToRunning(
                        repository=history.repository,
                        sha=merge.sha,
                        merged_at=merge.merged_at,
                        running_since=None,
                        seconds=seconds,
                        contained=False,
                    )
                )
    merge_times.sort(key=lambda m: (m.merged_at, m.repository, m.sha))

    # Median merge-to-running seconds.
    if merge_times:
        sorted_seconds = sorted(m.seconds for m in merge_times)
        median_merge_to_running_seconds = sorted_seconds[(len(sorted_seconds) - 1) // 2]
    else:
        median_merge_to_running_seconds = None
    median_over_bound = (
        median_merge_to_running_seconds is not None
        and median_merge_to_running_seconds
        > policy.median_merge_to_running_bound_seconds
    )

    # 5. Overall status and red reasons.
    statuses = [r.status for r in readings]
    if EnumRunningRevisionStatus.BEHIND in statuses:
        overall_status = EnumRunningRevisionStatus.BEHIND
    elif EnumRunningRevisionStatus.UNKNOWN in statuses:
        overall_status = EnumRunningRevisionStatus.UNKNOWN
    elif EnumRunningRevisionStatus.LAGGING in statuses:
        overall_status = EnumRunningRevisionStatus.LAGGING
    else:
        overall_status = EnumRunningRevisionStatus.CURRENT

    red_reasons: list[EnumRunningRevisionRedReason] = []
    if EnumRunningRevisionStatus.BEHIND in statuses:
        red_reasons.append(EnumRunningRevisionRedReason.BEHIND_PAST_THRESHOLD)
    if EnumRunningRevisionStatus.UNKNOWN in statuses:
        red_reasons.append(EnumRunningRevisionRedReason.UNKNOWN_REPOSITORY_READING)
    if median_over_bound:
        red_reasons.append(EnumRunningRevisionRedReason.MEDIAN_OVER_BOUND)
    red = bool(red_reasons)

    # Oldest not running: the uncontained merge with the smallest merged_at across repositories.
    oldest_overall: ModelOldestNotRunning | None = None
    for reading in readings:
        held = reading.oldest_not_running
        if held is None:
            continue
        if oldest_overall is None or held.merged_at < oldest_overall.merge.merged_at:
            oldest_overall = ModelOldestNotRunning(
                repository=reading.repository,
                merge=held,
                age_seconds=int((now - held.merged_at).total_seconds()),
            )

    current_build_id = current.manifest.build_id if current is not None else None
    current_source_hash_recorded = (
        current.manifest.source_hash is not None if current is not None else False
    )

    return ModelRunningRevisionReading(
        correlation_id=facts.correlation_id,
        observed_at=now,
        status=overall_status,
        repositories=tuple(readings),
        merge_times=tuple(merge_times),
        median_merge_to_running_seconds=median_merge_to_running_seconds,
        median_over_bound=median_over_bound,
        oldest_not_running=oldest_overall,
        red=red,
        red_reasons=tuple(red_reasons),
        current_build_id=current_build_id,
        current_source_hash_recorded=current_source_hash_recorded,
    )


class HandlerDevRunningRevision:
    """``handle(ModelRunningRevisionFacts) -> ModelRunningRevisionReading | ModelRunningRevisionRefused``.

    The runtime builds the handler with no arguments; the policy is then read from the
    node's overlay on the first request (no packaged fallback: an absent overlay raises).
    A test passes a policy in.
    """

    def __init__(self, policy: ModelRunningRevisionPolicy | None = None) -> None:
        self._policy = policy

    async def handle(self, request: ModelRunningRevisionFacts) -> Result:
        if self._policy is None:
            self._policy = load_dev_running_revision_policy()
        return read_running_revision(request, self._policy)
