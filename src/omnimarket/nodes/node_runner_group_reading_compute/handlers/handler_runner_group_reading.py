# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerGroupReading: whether a public repository can reach a runner group that holds lab runners.

COMPUTE handler: no clock read (the request carries the time), no state, no I/O. A refused or
unreadable org read, an empty read, a read older than its max age and a canary that was not read
read UNKNOWN and never WORKING.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    EnumRepositoryVisibility,
    ModelCanaryFacts,
    ModelCanaryRun,
    ModelGroupFact,
    ModelGroupRunner,
    ModelMergeGroupJobFact,
    ModelReadingSubject,
    ModelRepositoryFact,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingResult,
)

SUBJECT_GROUPS_READ = "groups_read"
SUBJECT_CANARY = "canary_public_workflow"
SUBJECT_READ_AGE = "read_age"
SUBJECT_PRIVATE_QUEUE_GROUP = "private_queue_group"
SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB = "private_queue_merge_group_job"

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def group_setting_subject(group_id: int) -> str:
    return f"group_setting:{group_id}"


def public_sees_lab_subject(group_id: int) -> str:
    return f"public_sees_lab:{group_id}"


class HandlerRunnerGroupReading:
    """handle(request) -> reading; subjects are ordered by group id, then the canary, then the read age."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        if request.read_failure is not None or not request.groups:
            why = (
                request.read_failure.value
                if request.read_failure is not None
                else "no-groups-returned"
            )
            return ModelRunnerGroupReadingResult(
                correlation_id=request.correlation_id,
                verdict=_U,
                subjects=(
                    ModelReadingSubject(
                        name=SUBJECT_GROUPS_READ,
                        verdict=_U,
                        reason=f"the runner groups were not read: {why}",
                    ),
                ),
                groups_read=0,
                read_failure=request.read_failure,
            )

        labels = request.settings.lab_runner_labels
        groups = sorted(request.groups, key=lambda g: g.group_id)
        lab_ids = {g.group_id for g in groups if self._holds_lab_runners(g, labels)}
        subjects: list[ModelReadingSubject] = []
        failing: set[int] = set()
        for group in groups:
            setting = self._group_setting(group, labels)
            sees = self._public_sees_lab(group, request.repositories, labels)
            subjects.extend((setting, sees))
            if _N in (setting.verdict, sees.verdict):
                failing.add(group.group_id)
        canary = self._canary(request, lab_ids, {g.group_id for g in groups})
        subjects.append(canary)
        if canary.verdict is _N:
            failing.update(
                run.picked_up_by_group_id
                for run in (request.canary.runs if request.canary else ())
                if run.picked_up_by_group_id in lab_ids
                and run.picked_up_by_group_id is not None
            )
        if request.settings.private_queue is not None:
            queue_group = self._private_queue_group(request)
            queue_job = self._private_queue_merge_group_job(request)
            subjects.extend((queue_group, queue_job))
            if _N in (queue_group.verdict, queue_job.verdict):
                failing.update(
                    g.group_id
                    for g in groups
                    if g.name == request.settings.private_queue.group_name
                )
        age = self._read_age(request)
        subjects.append(age)

        if age.verdict is _U:
            overall = _U
        elif any(s.verdict is _N for s in subjects):
            overall = _N
        elif any(s.verdict is _U for s in subjects):
            overall = _U
        else:
            overall = _W
        return ModelRunnerGroupReadingResult(
            correlation_id=request.correlation_id,
            verdict=overall,
            subjects=tuple(subjects),
            groups_read=len(groups),
            failing_group_ids=tuple(sorted(failing)),
        )

    @staticmethod
    def _holds_lab_runners(group: ModelGroupFact, labels: Sequence[str]) -> bool:
        wanted = set(labels)
        return any(wanted.intersection(runner.labels) for runner in group.runners)

    @staticmethod
    def _opens_public(group: ModelGroupFact, labels: Sequence[str]) -> bool:
        """AC1: the group allows public repositories and holds lab runners."""
        return (
            group.allows_public_repositories
            and HandlerRunnerGroupReading._holds_lab_runners(group, labels)
        )

    @staticmethod
    def _public_can_see(
        group: ModelGroupFact,
        repositories: Sequence[ModelRepositoryFact],
        labels: Sequence[str],
    ) -> list[str]:
        """AC2: the public repositories that can reach this group, when it holds lab runners."""
        if not (
            group.allows_public_repositories
            and HandlerRunnerGroupReading._holds_lab_runners(group, labels)
        ):
            return []
        public = sorted(
            r.name
            for r in repositories
            if r.visibility is EnumRepositoryVisibility.PUBLIC
        )
        if group.visibility is EnumGroupVisibility.ALL:
            return public
        if group.visibility is EnumGroupVisibility.SELECTED:
            chosen = set(group.selected_repositories)
            return [name for name in public if name in chosen]
        return []

    @staticmethod
    def _canary_starts(
        canary: ModelCanaryFacts | None, lab_ids: set[int]
    ) -> list[ModelCanaryRun]:
        if canary is None:
            return []
        return [r for r in canary.runs if r.picked_up_by_group_id in lab_ids]

    def _group_setting(
        self, group: ModelGroupFact, labels: Sequence[str]
    ) -> ModelReadingSubject:
        name = group_setting_subject(group.group_id)
        lab = self._holds_lab_runners(group, labels)
        if self._opens_public(group, labels):
            return ModelReadingSubject(
                name=name,
                verdict=_N,
                count=len(group.runners),
                reason=f"group {group.name} allows public repositories and holds lab runners",
            )
        if not lab:
            reason = f"group {group.name} holds no lab runner"
        else:
            reason = f"group {group.name} holds lab runners and does not allow public repositories"
        return ModelReadingSubject(name=name, verdict=_W, reason=reason)

    def _public_sees_lab(
        self,
        group: ModelGroupFact,
        repositories: Sequence[ModelRepositoryFact],
        labels: Sequence[str],
    ) -> ModelReadingSubject:
        name = public_sees_lab_subject(group.group_id)
        seen = self._public_can_see(group, repositories, labels)
        if seen:
            return ModelReadingSubject(
                name=name,
                verdict=_N,
                count=len(seen),
                reason=(
                    f"{len(seen)} public repositor{'y' if len(seen) == 1 else 'ies'} "
                    f"can see lab group {group.name}: {', '.join(seen)}"
                ),
            )
        return ModelReadingSubject(
            name=name,
            verdict=_W,
            count=0,
            reason=f"no public repository can see group {group.name} as a lab group",
        )

    def _canary(
        self,
        request: ModelRunnerGroupReadingRequest,
        lab_ids: set[int],
        known_ids: set[int],
    ) -> ModelReadingSubject:
        canary = request.canary
        if canary is None:
            return ModelReadingSubject(
                name=SUBJECT_CANARY,
                verdict=_U,
                reason="the canary public workflow was not read",
            )
        age = request.now - canary.read_at
        limit = timedelta(seconds=request.settings.max_age_seconds)
        if age < timedelta(0) or age > limit:
            return ModelReadingSubject(
                name=SUBJECT_CANARY,
                verdict=_U,
                reason=(
                    "the canary read is dated after the time it is judged at"
                    if age < timedelta(0)
                    else f"the canary read is {int(age.total_seconds())} s old, past the {request.settings.max_age_seconds} s limit"
                ),
            )
        started = self._canary_starts(canary, lab_ids)
        if started:
            return ModelReadingSubject(
                name=SUBJECT_CANARY,
                verdict=_N,
                count=len(started),
                reason=f"{len(started)} canary run(s) started on a group that holds lab runners",
            )
        unplaced = [
            r
            for r in canary.runs
            if r.picked_up_by_group_id is not None
            and r.picked_up_by_group_id not in known_ids
        ]
        if unplaced:
            return ModelReadingSubject(
                name=SUBJECT_CANARY,
                verdict=_U,
                count=len(unplaced),
                reason=f"{len(unplaced)} canary run(s) were picked up by a group the read did not return",
            )
        return ModelReadingSubject(
            name=SUBJECT_CANARY,
            verdict=_W,
            count=0,
            reason=f"{len(canary.runs)} canary run(s) read, none started on a lab group",
        )

    @staticmethod
    def _queue_members(group: ModelGroupFact, prefix: str) -> list[ModelGroupRunner]:
        return [r for r in group.runners if r.name.startswith(prefix + "-")]

    def _private_queue_group(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelReadingSubject:
        """OMN-23156 AC1, OMN-23387 AC1: the queue group reads private, closed to public, every pool online in it and on enough hosts."""
        queue = request.settings.private_queue
        assert queue is not None
        group = next((g for g in request.groups if g.name == queue.group_name), None)
        if group is None:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_GROUP,
                verdict=_N,
                count=0,
                reason=f"group {queue.group_name} is not in the organization's runner groups",
            )
        hard: list[str] = []
        short: list[str] = []
        if group.visibility is not EnumGroupVisibility.PRIVATE:
            hard.append(f"visibility reads {group.visibility.value}, not private")
        if group.allows_public_repositories:
            hard.append("it allows public repositories")
        online_total = 0
        unread_total = 0
        online_hosts: set[str] = set()
        for pool in queue.pools:
            members = self._queue_members(group, pool.runner_name_prefix)
            online = [m for m in members if m.online]
            online_total += len(online)
            unread_total += len([m for m in members if m.online is None])
            if online:
                online_hosts.add(pool.host)
            elsewhere = sorted(
                r.name
                for g in request.groups
                if g.group_id != group.group_id
                for r in self._queue_members(g, pool.runner_name_prefix)
            )
            if elsewhere:
                hard.append(
                    f"{len(elsewhere)} {pool.runner_name_prefix} runner(s) are registered in another group: {', '.join(elsewhere)}"
                )
            if len(online) < pool.expected_online_members:
                short.append(
                    f"{len(online)} of {pool.expected_online_members} {pool.runner_name_prefix} runners ({pool.host}) are online in it"
                )
        if len(online_hosts) < queue.min_hosts:
            short.append(
                f"online queue runners sit on {len(online_hosts)} host(s), fewer than the {queue.min_hosts} the overlay requires"
            )
        # A shortfall that runners with no read status could cover leaves the reading unknown, not failed.
        if hard or (short and not unread_total):
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_GROUP,
                verdict=_N,
                count=online_total,
                reason=f"group {group.name}: {'; '.join(hard + short)}",
            )
        if short:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_GROUP,
                verdict=_U,
                count=online_total,
                reason=f"group {group.name}: {unread_total} runner(s) carried no online status",
            )
        return ModelReadingSubject(
            name=SUBJECT_PRIVATE_QUEUE_GROUP,
            verdict=_W,
            count=online_total,
            reason=(
                f"group {group.name} reads private with {online_total} online queue runner(s) on "
                f"{len(online_hosts)} host(s) ({', '.join(sorted(online_hosts))}) and none registered elsewhere"
            ),
        )

    def _private_queue_merge_group_job(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelReadingSubject:
        """OMN-23156 AC2: a completed merge-group job of a private repository ran on a runner of the queue group."""
        queue = request.settings.private_queue
        assert queue is not None
        jobs: tuple[ModelMergeGroupJobFact, ...] | None = request.merge_group_jobs
        if jobs is None:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
                verdict=_U,
                reason="the merge-group job runs were not read",
            )
        group = next((g for g in request.groups if g.name == queue.group_name), None)
        if group is None:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
                verdict=_N,
                count=0,
                reason=f"group {queue.group_name} is not in the organization's runner groups",
            )
        on_pool = [j for j in jobs if j.runner_group_id == group.group_id]
        public = [
            j
            for j in on_pool
            if j.repository_visibility is not EnumRepositoryVisibility.PRIVATE
        ]
        if public:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
                verdict=_N,
                count=len(public),
                reason=f"{len(public)} merge-group job(s) of a non-private repository ran on group {group.name}",
            )
        prefixes = tuple(p.runner_name_prefix + "-" for p in queue.pools)
        proven = [
            j
            for j in on_pool
            if j.conclusion == "success"
            and j.runner_name is not None
            and j.runner_name.startswith(prefixes)
        ]
        if not proven:
            return ModelReadingSubject(
                name=SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
                verdict=_N,
                count=0,
                reason=(
                    f"no completed merge-group job of a private repository ran on a "
                    f"{'/'.join(p.runner_name_prefix for p in queue.pools)} runner of group {group.name} ({len(jobs)} job(s) read)"
                ),
            )
        first = proven[0]
        return ModelReadingSubject(
            name=SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
            verdict=_W,
            count=len(proven),
            reason=f"run {first.run_id} completed on {first.runner_name} of group {group.name}",
        )

    @staticmethod
    def _read_age(request: ModelRunnerGroupReadingRequest) -> ModelReadingSubject:
        elapsed = request.now - request.read_at
        limit = request.settings.max_age_seconds
        age = int(elapsed.total_seconds())
        if elapsed < timedelta(0):
            return ModelReadingSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                reason="the read is dated after the time it is judged at",
            )
        if elapsed > timedelta(seconds=limit):
            return ModelReadingSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                count=age,
                reason=f"the read is {age} s old, past the {limit} s limit",
            )
        return ModelReadingSubject(
            name=SUBJECT_READ_AGE,
            verdict=_W,
            count=age,
            reason=f"the read is {age} s old, inside the {limit} s limit",
        )


__all__: list[str] = [
    "SUBJECT_CANARY",
    "SUBJECT_GROUPS_READ",
    "SUBJECT_PRIVATE_QUEUE_GROUP",
    "SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB",
    "SUBJECT_READ_AGE",
    "HandlerRunnerGroupReading",
    "group_setting_subject",
    "public_sees_lab_subject",
]
