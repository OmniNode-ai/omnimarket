# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23156): the private queue group reads private with four online queue runners, and a named break turns it red."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.handlers.handler_runner_group_reading import (
    SUBJECT_PRIVATE_QUEUE_GROUP,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    ModelGroupFact,
    ModelGroupRunner,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    QUEUE_GROUP_ID,
    QUEUE_PREFIX,
    SECOND_QUEUE_PREFIX,
    queue_group,
    queue_request,
    request,
    settings,
)

_HANDLER = HandlerRunnerGroupReading()
_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def _queue(result: ModelRunnerGroupReadingResult) -> Any:
    return next(s for s in result.subjects if s.name == SUBJECT_PRIVATE_QUEUE_GROUP)


def test_private_queue_group_readback_prints_private_with_four_online_members() -> None:
    result = _HANDLER.handle(queue_request())

    subject = _queue(result)
    assert subject.verdict is _W
    assert subject.count == 8
    assert (
        "reads private with 8 online queue runner(s) on 2 host(s) (h051, h053)"
        in subject.reason
    )
    assert result.verdict is _W


def test_private_queue_group_readback_fails_when_the_group_visibility_is_not_private() -> (
    None
):
    for visibility in (EnumGroupVisibility.ALL, EnumGroupVisibility.SELECTED):
        result = _HANDLER.handle(queue_request(queue_group(visibility=visibility)))
        assert _queue(result).verdict is _N
        assert (
            f"visibility reads {visibility.value}, not private" in _queue(result).reason
        )
        assert result.verdict is _N
        assert QUEUE_GROUP_ID in result.failing_group_ids


def test_private_queue_group_readback_fails_when_the_group_allows_public_repositories() -> (
    None
):
    result = _HANDLER.handle(queue_request(queue_group(allows_public=True)))

    assert _queue(result).verdict is _N
    assert "it allows public repositories" in _queue(result).reason


def test_private_queue_group_readback_fails_with_three_of_four_online() -> None:
    result = _HANDLER.handle(
        queue_request(queue_group(online=(True, True, True, False)))
    )

    assert _queue(result).verdict is _N
    assert "3 of 4" in _queue(result).reason


def test_private_queue_group_readback_is_unknown_when_a_runner_carries_no_status() -> (
    None
):
    result = _HANDLER.handle(
        queue_request(queue_group(online=(True, True, True, None)))
    )

    assert _queue(result).verdict is _U
    assert result.verdict.blocks is True


def test_private_queue_group_readback_fails_when_the_group_is_absent() -> None:
    # Before the operator places the group the reading fails; it never reads WORKING.
    result = _HANDLER.handle(queue_request(queue_group(name="some-other-group")))

    assert _queue(result).verdict is _N
    assert "is not in the organization's runner groups" in _queue(result).reason


def test_private_queue_group_readback_fails_when_a_queue_runner_stays_in_another_group() -> (
    None
):
    shared = ModelGroupFact(
        group_id=3,
        name="shared-group",
        visibility=EnumGroupVisibility.ALL,
        allows_public_repositories=False,
        runners=(
            ModelGroupRunner(
                name=f"{QUEUE_PREFIX}-4", labels=("self-hosted",), online=True
            ),
        ),
    )
    full = queue_group()
    # The queue group lists four online members, and one of the same names is registered in the shared group too.
    req = queue_request(full, shared)
    result = _HANDLER.handle(req)

    assert _queue(result).verdict is _N
    assert f"registered in another group: {QUEUE_PREFIX}-4" in _queue(result).reason


def test_private_queue_group_readback_is_skipped_when_the_overlay_names_no_queue() -> (
    None
):
    result = _HANDLER.handle(request())

    assert SUBJECT_PRIVATE_QUEUE_GROUP not in {s.name for s in result.subjects}


def test_private_queue_group_readback_overlay_names_the_group_and_the_runners() -> None:
    queue = settings(queue=True).private_queue
    assert queue is not None
    assert queue.group_name == "omni-private-queue"
    assert {p.runner_name_prefix for p in queue.pools} == {
        QUEUE_PREFIX,
        SECOND_QUEUE_PREFIX,
    }
    assert all(p.expected_online_members == 4 for p in queue.pools)


class _VisibilityIgnored(HandlerRunnerGroupReading):
    """Injected break: the visibility of the queue group is never judged."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        groups = tuple(
            g.model_copy(update={"visibility": EnumGroupVisibility.PRIVATE})
            for g in request.groups
        )
        return super().handle(request.model_copy(update={"groups": groups}))


class _OnlineIgnored(HandlerRunnerGroupReading):
    """Injected break: every queue runner counts as online."""

    def handle(
        self, request: ModelRunnerGroupReadingRequest
    ) -> ModelRunnerGroupReadingResult:
        groups = tuple(
            g.model_copy(
                update={
                    "runners": tuple(
                        r.model_copy(update={"online": True}) for r in g.runners
                    )
                }
            )
            for g in request.groups
        )
        return super().handle(request.model_copy(update={"groups": groups}))


def _assert_readback(handler: HandlerRunnerGroupReading) -> None:
    for visibility in (EnumGroupVisibility.ALL, EnumGroupVisibility.SELECTED):
        assert (
            _queue(
                handler.handle(queue_request(queue_group(visibility=visibility)))
            ).verdict
            is _N
        )
    assert (
        _queue(
            handler.handle(queue_request(queue_group(online=(True, True, True, False))))
        ).verdict
        is _N
    )
    assert _queue(handler.handle(queue_request())).verdict is _W


def test_private_queue_group_readback_passes_for_the_real_handler() -> None:
    _assert_readback(HandlerRunnerGroupReading())


@pytest.mark.parametrize("broken", [_VisibilityIgnored, _OnlineIgnored])
def test_private_queue_group_readback_turns_red_under_the_injected_break(
    broken: type[HandlerRunnerGroupReading],
) -> None:
    with pytest.raises(AssertionError):
        _assert_readback(broken())
