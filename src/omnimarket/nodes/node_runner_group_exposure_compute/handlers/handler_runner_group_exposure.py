# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRunnerGroupExposure: whether a runner group can be reached from a public origin.

COMPUTE handler: no clock read (the request carries the time), no state, no I/O. A refused or
unreadable org read, and a read older than its max age, read UNKNOWN and never WORKING.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.runner_group_exposure import (
    ModelExposureSubject,
    ModelRunnerGroupExposureReading,
    ModelRunnerGroupExposureRequest,
)

SUBJECT_PUBLIC_REACH_FILES = "g7_public_reach_files"
SUBJECT_PINNED_EQUALS_FILES = "g7_pinned_equals_files"
SUBJECT_ALLOWS_PUBLIC = "g7_allows_public"
SUBJECT_PUBLIC_JOBS_WINDOW = "g7_public_jobs_window"
SUBJECT_PUBLIC_REPOSITORY_JOBS = "g7_public_repository_jobs"
SUBJECT_READ_AGE = "g7_read_age"
SUBJECTS: tuple[str, ...] = (
    SUBJECT_PUBLIC_REACH_FILES,
    SUBJECT_PINNED_EQUALS_FILES,
    SUBJECT_ALLOWS_PUBLIC,
    SUBJECT_PUBLIC_JOBS_WINDOW,
    SUBJECT_PUBLIC_REPOSITORY_JOBS,
    SUBJECT_READ_AGE,
)

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


class HandlerRunnerGroupExposure:
    """handle(request) -> reading; the six subjects are in a fixed order."""

    def handle(
        self, request: ModelRunnerGroupExposureRequest
    ) -> ModelRunnerGroupExposureReading:
        if request.read_failure is not None or request.group is None:
            why = (
                request.read_failure.value
                if request.read_failure is not None
                else "no-group-facts"
            )
            subjects = tuple(
                ModelExposureSubject(
                    name=name, verdict=_U, reason=f"the group was not read: {why}"
                )
                for name in SUBJECTS
            )
            return ModelRunnerGroupExposureReading(
                correlation_id=request.correlation_id,
                verdict=_U,
                subjects=subjects,
                read_failure=request.read_failure,
            )

        subjects = (
            self._public_reach_files(request),
            self._pinned_equals_files(request),
            self._allows_public(request),
            self._public_jobs_window(request),
            self._public_repository_jobs(request),
            self._read_age(request),
        )
        by_name = {s.name: s for s in subjects}
        if by_name[SUBJECT_READ_AGE].verdict is _U:
            overall = _U
        elif any(s.verdict is _N for s in subjects):
            overall = _N
        elif any(s.verdict is _U for s in subjects):
            overall = _U
        else:
            overall = _W
        return ModelRunnerGroupExposureReading(
            correlation_id=request.correlation_id, verdict=overall, subjects=subjects
        )

    @staticmethod
    def _public_reach_files(
        request: ModelRunnerGroupExposureRequest,
    ) -> ModelExposureSubject:
        events = set(request.settings.public_reach_events)
        reachable = [
            f
            for f in request.files
            if f.selects_group_label and events.intersection(f.triggers)
        ]
        names = ", ".join(sorted(f.identity for f in reachable))
        return ModelExposureSubject(
            name=SUBJECT_PUBLIC_REACH_FILES,
            verdict=_W if not reachable else _N,
            count=len(reachable),
            reason=(
                "no file that selects a group label has a public-origin trigger"
                if not reachable
                else f"{len(reachable)} file(s) select a group label and a public-origin trigger: {names}"
            ),
        )

    @staticmethod
    def _pinned_equals_files(
        request: ModelRunnerGroupExposureRequest,
    ) -> ModelExposureSubject:
        group = request.group
        assert group is not None
        files = {f.identity for f in request.files if f.selects_group_label}
        if not group.restricted_to_workflows:
            return ModelExposureSubject(
                name=SUBJECT_PINNED_EQUALS_FILES,
                verdict=_N,
                count=len(files),
                reason="the group is not restricted to selected workflows",
            )
        pinned = set(group.selected_workflows)
        unpinned = sorted(files - pinned)
        dangling = sorted(pinned - files)
        if not unpinned and not dangling:
            return ModelExposureSubject(
                name=SUBJECT_PINNED_EQUALS_FILES,
                verdict=_W,
                count=0,
                reason=f"the {len(pinned)} pinned workflow(s) equal the files that select a group label",
            )
        return ModelExposureSubject(
            name=SUBJECT_PINNED_EQUALS_FILES,
            verdict=_N,
            count=len(unpinned) + len(dangling),
            reason=(
                f"{len(unpinned)} file(s) select a group label and are not pinned: {', '.join(unpinned)}; "
                f"{len(dangling)} pin(s) match no such file: {', '.join(dangling)}"
            ),
        )

    @staticmethod
    def _allows_public(
        request: ModelRunnerGroupExposureRequest,
    ) -> ModelExposureSubject:
        group = request.group
        assert group is not None
        return ModelExposureSubject(
            name=SUBJECT_ALLOWS_PUBLIC,
            verdict=_N if group.allows_public_repositories else _W,
            reason=(
                "the group allows public repositories"
                if group.allows_public_repositories
                else "the group does not allow public repositories"
            ),
        )

    @staticmethod
    def _public_jobs_window(
        request: ModelRunnerGroupExposureRequest,
    ) -> ModelExposureSubject:
        events = set(request.settings.public_reach_events)
        public = [j for j in request.jobs if j.run_event in events or j.head_is_fork]
        return ModelExposureSubject(
            name=SUBJECT_PUBLIC_JOBS_WINDOW,
            verdict=_W if not public else _N,
            count=len(public),
            reason=(
                f"{len(request.jobs)} job(s) on the group, none of public origin"
                if not public
                else f"{len(public)} of {len(request.jobs)} job(s) on the group are of public origin"
            ),
        )

    @staticmethod
    def _public_repository_jobs(
        request: ModelRunnerGroupExposureRequest,
    ) -> ModelExposureSubject:
        """The drain readback: no run queued or in progress on the group for a public repository, whatever its event."""
        public = [j for j in request.jobs if j.repository_private is False]
        unsaid = [j for j in request.jobs if j.repository_private is None]
        if public:
            return ModelExposureSubject(
                name=SUBJECT_PUBLIC_REPOSITORY_JOBS,
                verdict=_N,
                count=len(public),
                reason=f"{len(public)} of {len(request.jobs)} job(s) on the group belong to a public repository",
            )
        if unsaid:
            return ModelExposureSubject(
                name=SUBJECT_PUBLIC_REPOSITORY_JOBS,
                verdict=_U,
                count=len(unsaid),
                reason=f"{len(unsaid)} of {len(request.jobs)} job(s) on the group did not say whether their repository is public",
            )
        return ModelExposureSubject(
            name=SUBJECT_PUBLIC_REPOSITORY_JOBS,
            verdict=_W,
            count=0,
            reason=f"{len(request.jobs)} job(s) on the group, none of a public repository",
        )

    @staticmethod
    def _read_age(request: ModelRunnerGroupExposureRequest) -> ModelExposureSubject:
        age = request.now - request.read_at
        limit = timedelta(seconds=request.settings.max_age_seconds)
        if age < timedelta(0):
            return ModelExposureSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                reason="the read is dated after the time it is judged at",
            )
        if age > limit:
            return ModelExposureSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                count=int(age.total_seconds()),
                reason=f"the read is {int(age.total_seconds())} s old, past the {request.settings.max_age_seconds} s limit",
            )
        return ModelExposureSubject(
            name=SUBJECT_READ_AGE,
            verdict=_W,
            count=int(age.total_seconds()),
            reason=f"the read is {int(age.total_seconds())} s old, inside the {request.settings.max_age_seconds} s limit",
        )


__all__: list[str] = ["SUBJECTS", "HandlerRunnerGroupExposure"]
