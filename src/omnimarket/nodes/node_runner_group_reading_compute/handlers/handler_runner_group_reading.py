# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerGroupReading: whether a public repository can reach a runner group that holds lab runners.

COMPUTE handler: no clock read (the request carries the time), no state, no I/O. A refused or
unreadable org read, an empty read, a read older than its max age and a canary that was not read
read UNKNOWN and never WORKING.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    EnumRepositoryVisibility,
    ModelCanaryFacts,
    ModelCanaryRun,
    ModelGroupFact,
    ModelReadingSubject,
    ModelRepositoryFact,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingResult,
)

SUBJECT_GROUPS_READ = "groups_read"
SUBJECT_CANARY = "canary_public_workflow"
SUBJECT_READ_AGE = "read_age"

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
        failing.update(
            run.picked_up_by_group_id
            for run in (request.canary.runs if request.canary else ())
            if run.picked_up_by_group_id in lab_ids
            and run.picked_up_by_group_id is not None
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
    def _read_age(request: ModelRunnerGroupReadingRequest) -> ModelReadingSubject:
        age = _age(request.now, request.read_at)
        limit = request.settings.max_age_seconds
        if age < 0:
            return ModelReadingSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                reason="the read is dated after the time it is judged at",
            )
        if age > limit:
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


def _age(now: datetime, read_at: datetime) -> int:
    return int((now - read_at).total_seconds())


__all__: list[str] = [
    "SUBJECT_CANARY",
    "SUBJECT_GROUPS_READ",
    "SUBJECT_READ_AGE",
    "HandlerRunnerGroupReading",
    "group_setting_subject",
    "public_sees_lab_subject",
]
