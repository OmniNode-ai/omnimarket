# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-22949: after the group command the group reads allows_public_repositories false and no run
is in progress or queued on a group-7 runner for a public repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.runner_group_exposure import (
    EnumExposureReadFailure,
    ModelExposureSettings,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupExposureReading,
    ModelRunnerGroupExposureRequest,
    ModelRunnerGroupFacts,
)
from omnimarket.nodes.node_runner_group_exposure_compute.handlers.handler_runner_group_exposure import (
    SUBJECT_ALLOWS_PUBLIC,
    SUBJECT_PUBLIC_REPOSITORY_JOBS,
    SUBJECTS,
    HandlerRunnerGroupExposure,
)

_NOW = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
_REPO = "example-org/example-repo"
_FILE = ModelGroupWorkflowFile(
    repository=_REPO,
    path=".github/workflows/build.yml",
    ref="main",
    selects_group_label=True,
    triggers=("push",),
)
_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def _job(run_id: int, *, private: bool | None, event: str = "push") -> ModelGroupJob:
    return ModelGroupJob(
        run_id=run_id,
        run_event=event,
        workflow_ref=_FILE.path,
        repository=_REPO,
        repository_private=private,
    )


def _read(
    *, allows_public: bool, jobs: tuple[ModelGroupJob, ...]
) -> ModelRunnerGroupExposureReading:
    return HandlerRunnerGroupExposure().handle(
        ModelRunnerGroupExposureRequest(
            correlation_id="drain-readback",
            now=_NOW,
            read_at=_NOW - timedelta(seconds=30),
            settings=ModelExposureSettings(
                public_reach_events=("pull_request", "pull_request_target"),
                max_age_seconds=1800,
            ),
            group=ModelRunnerGroupFacts(
                group_id=7,
                allows_public_repositories=allows_public,
                restricted_to_workflows=True,
                selected_workflows=(_FILE.identity,),
                repositories=(_REPO,),
            ),
            files=(_FILE,),
            jobs=jobs,
        )
    )


def _verdict(reading: ModelRunnerGroupExposureReading, name: str) -> EnumReadingVerdict:
    return next(s.verdict for s in reading.subjects if s.name == name)


def test_drained_group_closed_to_public_with_no_public_repository_run_reads_working() -> (
    None
):
    reading = _read(allows_public=False, jobs=(_job(1, private=True),))

    assert _verdict(reading, SUBJECT_ALLOWS_PUBLIC) is _W
    assert _verdict(reading, SUBJECT_PUBLIC_REPOSITORY_JOBS) is _W
    assert reading.verdict is _W


def test_drained_group_with_no_run_at_all_reads_working() -> None:
    reading = _read(allows_public=False, jobs=())

    assert _verdict(reading, SUBJECT_PUBLIC_REPOSITORY_JOBS) is _W
    assert reading.verdict is _W


def test_control_a_group_still_open_to_public_repositories_reads_not_working() -> None:
    reading = _read(allows_public=True, jobs=())

    assert _verdict(reading, SUBJECT_ALLOWS_PUBLIC) is _N
    assert reading.verdict is _N


def test_control_a_queued_or_running_public_repository_run_blocks_the_drain_whatever_its_event() -> (
    None
):
    """A push on a public repository is not a public-origin event, yet it is still a public-repository run."""
    reading = _read(
        allows_public=False,
        jobs=(_job(1, private=True), _job(2, private=False, event="push")),
    )
    subject = next(
        s for s in reading.subjects if s.name == SUBJECT_PUBLIC_REPOSITORY_JOBS
    )

    assert subject.verdict is _N
    assert subject.count == 1
    assert reading.verdict is _N


def test_control_a_run_that_did_not_say_its_repository_visibility_never_reads_drained() -> (
    None
):
    reading = _read(allows_public=False, jobs=(_job(1, private=None),))

    assert _verdict(reading, SUBJECT_PUBLIC_REPOSITORY_JOBS) is _U
    assert reading.verdict is _U


def test_control_an_unread_group_reads_unknown_on_the_drain_subject() -> None:
    reading = HandlerRunnerGroupExposure().handle(
        ModelRunnerGroupExposureRequest(
            correlation_id="drain-readback",
            now=_NOW,
            read_at=_NOW,
            settings=ModelExposureSettings(
                public_reach_events=("pull_request",), max_age_seconds=1800
            ),
            read_failure=EnumExposureReadFailure.ORG_READ_DENIED,
        )
    )

    assert SUBJECT_PUBLIC_REPOSITORY_JOBS in SUBJECTS
    assert _verdict(reading, SUBJECT_PUBLIC_REPOSITORY_JOBS) is _U
    assert reading.verdict is _U
