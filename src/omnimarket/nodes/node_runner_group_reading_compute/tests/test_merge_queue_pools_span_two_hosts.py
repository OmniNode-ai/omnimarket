# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23387): the merge-queue runners are declared on at least two hosts, and a single-host declaration is refused."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.handlers.handler_runner_group_reading import (
    SUBJECT_PRIVATE_QUEUE_GROUP,
    SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    ModelPrivateQueueSettings,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    QUEUE_PREFIX,
    SECOND_QUEUE_PREFIX,
    overlay,
    queue_group,
    queue_job,
    queue_request,
    settings,
)

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_NONE_ONLINE = (False, False, False, False)


def _queue_settings() -> ModelPrivateQueueSettings:
    queue = settings(queue=True).private_queue
    assert queue is not None
    return queue


def _subject(result: ModelRunnerGroupReadingResult, name: str) -> Any:
    return next(s for s in result.subjects if s.name == name)


def test_merge_queue_pools_span_two_hosts_in_the_overlay() -> None:
    queue = _queue_settings()

    assert {p.host for p in queue.pools} == {"h051", "h053"}
    assert queue.min_hosts == 2


def test_merge_queue_pools_span_two_hosts_refuses_a_declaration_on_one_host() -> None:
    declared = overlay()["private_queue"]
    one_host = {
        **declared,
        "pools": [{**declared["pools"][0]}, {**declared["pools"][1], "host": "h053"}],
    }

    with pytest.raises(ValidationError, match="resolve to 1 host"):
        ModelPrivateQueueSettings.model_validate(one_host)


def test_merge_queue_pools_span_two_hosts_refuses_a_floor_below_two() -> None:
    declared = overlay()["private_queue"]

    with pytest.raises(ValidationError):
        ModelPrivateQueueSettings.model_validate({**declared, "min_hosts": 1})


def test_merge_queue_pools_span_two_hosts_refuses_a_single_pool() -> None:
    declared = overlay()["private_queue"]

    with pytest.raises(ValidationError, match="resolve to 1 host"):
        ModelPrivateQueueSettings.model_validate(
            {**declared, "pools": declared["pools"][:1]}
        )


def test_merge_queue_pools_span_two_hosts_refuses_two_pools_with_one_prefix() -> None:
    declared = overlay()["private_queue"]
    twin = {
        **declared,
        "pools": [
            declared["pools"][0],
            {**declared["pools"][1], "runner_name_prefix": QUEUE_PREFIX},
        ],
    }

    with pytest.raises(ValidationError, match="same runner name prefix"):
        ModelPrivateQueueSettings.model_validate(twin)


def test_merge_queue_pools_span_two_hosts_reads_working_with_both_hosts_online() -> (
    None
):
    result = HandlerRunnerGroupReading().handle(queue_request())

    assert _subject(result, SUBJECT_PRIVATE_QUEUE_GROUP).verdict is _W


@pytest.mark.parametrize(
    ("first", "second", "stopped"),
    [(_NONE_ONLINE, None, "h053"), (None, _NONE_ONLINE, "h051")],
)
def test_merge_queue_pools_span_two_hosts_reads_not_working_with_one_host_stopped(
    first: tuple[bool, ...] | None, second: tuple[bool, ...] | None, stopped: str
) -> None:
    group = queue_group(
        online=first or (True, True, True, True),
        second_online=second or (True, True, True, True),
    )

    result = HandlerRunnerGroupReading().handle(queue_request(group))

    reason = _subject(result, SUBJECT_PRIVATE_QUEUE_GROUP).reason
    assert _subject(result, SUBJECT_PRIVATE_QUEUE_GROUP).verdict is _N
    assert f"({stopped}) are online in it" in reason
    assert "sit on 1 host(s), fewer than the 2" in reason


@pytest.mark.parametrize("survivor", [QUEUE_PREFIX, SECOND_QUEUE_PREFIX])
def test_merge_queue_pools_span_two_hosts_a_job_proves_either_pool(
    survivor: str,
) -> None:
    """With one host stopped, a merge-group job on the other host's runner is still a proof."""
    result = HandlerRunnerGroupReading().handle(
        queue_request(jobs=(queue_job(runner_name=f"{survivor}-3"),))
    )

    assert _subject(result, SUBJECT_PRIVATE_QUEUE_MERGE_GROUP_JOB).verdict is _W


class _OneHostSuffices(HandlerRunnerGroupReading):
    """Injected break: a pool with no online runner is dropped from the declaration and the floor is one host."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        queue = request.settings.private_queue
        assert queue is not None
        group = next(g for g in request.groups if g.name == queue.group_name)
        live = tuple(
            p
            for p in queue.pools
            if any(
                r.online
                for r in group.runners
                if r.name.startswith(p.runner_name_prefix)
            )
        )
        loosened = queue.model_copy(update={"pools": live, "min_hosts": 1})
        settings_ = request.settings.model_copy(update={"private_queue": loosened})
        return super().handle(request.model_copy(update={"settings": settings_}))


def _assert_a_stopped_host_is_named(handler: HandlerRunnerGroupReading) -> None:
    stopped = queue_request(queue_group(second_online=_NONE_ONLINE))
    assert _subject(handler.handle(stopped), SUBJECT_PRIVATE_QUEUE_GROUP).verdict is _N
    assert (
        _subject(handler.handle(queue_request()), SUBJECT_PRIVATE_QUEUE_GROUP).verdict
        is _W
    )


def test_merge_queue_pools_span_two_hosts_passes_for_the_real_handler() -> None:
    _assert_a_stopped_host_is_named(HandlerRunnerGroupReading())


def test_merge_queue_pools_span_two_hosts_turns_red_under_the_injected_break() -> None:
    with pytest.raises(AssertionError):
        _assert_a_stopped_host_is_named(_OneHostSuffices())
