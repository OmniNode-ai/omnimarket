# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-23156): a merge-group job of a private repository completed on a runner of the queue group, and a named break turns it red."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.handlers.handler_runner_group_reading import (
    SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumRepositoryVisibility,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    QUEUE_PREFIX,
    queue_job,
    queue_request,
)

_HANDLER = HandlerRunnerGroupReading()
_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def _job(result: ModelRunnerGroupReadingResult) -> Any:
    return next(
        s for s in result.subjects if s.name == SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB
    )


def test_merge_group_job_on_private_pool_reads_working_and_names_the_runner() -> None:
    result = _HANDLER.handle(queue_request())

    assert _job(result).verdict is _W
    assert f"run 9001 completed on {QUEUE_PREFIX}-2" in _job(result).reason


def test_merge_group_job_on_private_pool_is_unknown_when_the_runs_were_not_read() -> (
    None
):
    req = queue_request().model_copy(update={"merge_group_jobs": None})
    result = _HANDLER.handle(req)

    assert _job(result).verdict is _U
    assert result.verdict.blocks is True


def test_merge_group_job_on_private_pool_fails_with_no_job_on_the_pool() -> None:
    result = _HANDLER.handle(queue_request(jobs=()))

    assert _job(result).verdict is _N
    assert "no completed merge-group job" in _job(result).reason


def test_merge_group_job_on_private_pool_fails_for_a_job_that_did_not_succeed() -> None:
    result = _HANDLER.handle(queue_request(jobs=(queue_job(conclusion="failure"),)))

    assert _job(result).verdict is _N


def test_merge_group_job_on_private_pool_fails_for_a_job_on_another_group() -> None:
    result = _HANDLER.handle(queue_request(jobs=(queue_job(runner_group_id=3),)))

    assert _job(result).verdict is _N


def test_merge_group_job_on_private_pool_fails_for_a_runner_outside_the_pool() -> None:
    result = _HANDLER.handle(
        queue_request(jobs=(queue_job(runner_name="some-other-runner-1"),))
    )

    assert _job(result).verdict is _N


def test_merge_group_job_on_private_pool_fails_when_a_public_job_ran_on_the_pool() -> (
    None
):
    jobs = (
        queue_job(),
        queue_job(
            run_id=9002,
            repository="example-public",
            repository_visibility=EnumRepositoryVisibility.PUBLIC,
        ),
    )
    result = _HANDLER.handle(queue_request(jobs=jobs))

    assert _job(result).verdict is _N
    assert "non-private repository ran on group" in _job(result).reason
    assert result.verdict is _N


def test_merge_group_job_on_private_pool_a_public_repository_job_is_no_proof() -> None:
    result = _HANDLER.handle(
        queue_request(
            jobs=(queue_job(repository_visibility=EnumRepositoryVisibility.PUBLIC),)
        )
    )

    assert _job(result).verdict is _N


class _AnyJobProves(HandlerRunnerGroupReading):
    """Injected break: a job of any group and any result counts as the proof."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        queue = request.settings.private_queue
        assert queue is not None
        group = next(g for g in request.groups if g.name == queue.group_name)
        jobs = tuple(
            j.model_copy(
                update={
                    "runner_group_id": group.group_id,
                    "conclusion": "success",
                    "runner_name": f"{queue.pools[0].runner_name_prefix}-1",
                }
            )
            for j in request.merge_group_jobs or ()
        )
        return super().handle(request.model_copy(update={"merge_group_jobs": jobs}))


class _UnreadIsWorking(HandlerRunnerGroupReading):
    """Injected break: runs that were not read are taken as a proven job."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        if request.merge_group_jobs is None:
            request = request.model_copy(update={"merge_group_jobs": (queue_job(),)})
        return super().handle(request)


def _assert_proof(handler: HandlerRunnerGroupReading) -> None:
    assert _job(handler.handle(queue_request())).verdict is _W
    assert (
        _job(
            handler.handle(queue_request(jobs=(queue_job(runner_group_id=3),)))
        ).verdict
        is _N
    )
    assert (
        _job(
            handler.handle(queue_request(jobs=(queue_job(conclusion="failure"),)))
        ).verdict
        is _N
    )
    unread = queue_request().model_copy(update={"merge_group_jobs": None})
    assert _job(handler.handle(unread)).verdict is _U


def test_merge_group_job_on_private_pool_passes_for_the_real_handler() -> None:
    _assert_proof(HandlerRunnerGroupReading())


@pytest.mark.parametrize("broken", [_AnyJobProves, _UnreadIsWorking])
def test_merge_group_job_on_private_pool_turns_red_under_the_injected_break(
    broken: type[HandlerRunnerGroupReading],
) -> None:
    with pytest.raises(AssertionError):
        _assert_proof(broken())
