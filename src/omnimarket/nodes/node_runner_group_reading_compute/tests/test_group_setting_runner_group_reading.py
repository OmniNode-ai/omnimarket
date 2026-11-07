# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22947): the group-setting test fails for a group that allows public repositories and holds lab runners."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    ModelReadingSubject,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    CLOSED_GROUP_ID,
    LAB_GROUP_ID,
    closed_groups,
    hosted_group,
    lab_group,
    request,
)

_HANDLER = HandlerRunnerGroupReading()


def _subject(result: ModelRunnerGroupReadingResult, name: str) -> ModelReadingSubject:
    return next(s for s in result.subjects if s.name == name)


def test_group_setting_fails_for_a_group_that_allows_public_and_holds_lab_runners() -> (
    None
):
    groups = (lab_group(group_id=LAB_GROUP_ID, allows_public=True), hosted_group())
    result = _HANDLER.handle(request(groups))

    subject = _subject(result, f"group_setting:{LAB_GROUP_ID}")
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.failing_group_ids == (LAB_GROUP_ID,)


def test_group_setting_passes_for_the_closed_groups() -> None:
    result = _HANDLER.handle(request(closed_groups()))

    settings = [s for s in result.subjects if s.name.startswith("group_setting:")]
    assert len(settings) == 4
    assert {s.verdict for s in settings} == {EnumReadingVerdict.WORKING}
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.failing_group_ids == ()


def test_group_setting_passes_for_a_hosted_group_that_allows_public() -> None:
    result = _HANDLER.handle(request((hosted_group(group_id=9, allows_public=True),)))

    assert _subject(result, "group_setting:9").verdict is EnumReadingVerdict.WORKING


def test_group_setting_names_only_the_open_group_among_closed_ones() -> None:
    groups = (
        lab_group(group_id=CLOSED_GROUP_ID, allows_public=False),
        lab_group(group_id=LAB_GROUP_ID, allows_public=True),
    )
    result = _HANDLER.handle(request(groups))

    assert result.failing_group_ids == (LAB_GROUP_ID,)
    assert (
        _subject(result, f"group_setting:{CLOSED_GROUP_ID}").verdict
        is EnumReadingVerdict.WORKING
    )
