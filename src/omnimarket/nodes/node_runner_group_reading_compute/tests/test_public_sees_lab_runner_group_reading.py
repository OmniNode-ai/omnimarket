# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22947): a fixture where a public repository can see a lab group flips the reading to NOT WORKING within one probe."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    ModelGroupFact,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    LAB_GROUP_ID,
    closed_groups,
    lab_group,
    request,
)

_HANDLER = HandlerRunnerGroupReading()
_SUBJECT = f"public_sees_lab:{LAB_GROUP_ID}"


def _verdict(
    groups: tuple[ModelGroupFact, ...],
) -> tuple[EnumReadingVerdict, EnumReadingVerdict]:
    result = _HANDLER.handle(request(groups))
    subject = next(s for s in result.subjects if s.name == _SUBJECT)
    return result.verdict, subject.verdict


def test_public_sees_lab_flips_the_reading_in_one_probe() -> None:
    before = _HANDLER.handle(request(closed_groups()))
    assert before.verdict is EnumReadingVerdict.WORKING

    opened = (lab_group(group_id=LAB_GROUP_ID, allows_public=True),)
    overall, subject = _verdict(opened)

    assert subject is EnumReadingVerdict.NOT_WORKING
    assert overall is EnumReadingVerdict.NOT_WORKING


def test_public_sees_lab_through_a_selected_list_naming_a_public_repository() -> None:
    groups = (
        lab_group(
            group_id=LAB_GROUP_ID,
            allows_public=True,
            visibility=EnumGroupVisibility.SELECTED,
            selected=("example-public",),
        ),
    )
    overall, subject = _verdict(groups)

    assert subject is EnumReadingVerdict.NOT_WORKING
    assert overall is EnumReadingVerdict.NOT_WORKING


def test_public_sees_lab_is_working_when_the_selected_list_holds_only_private_repositories() -> (
    None
):
    groups = (
        lab_group(
            group_id=LAB_GROUP_ID,
            allows_public=True,
            visibility=EnumGroupVisibility.SELECTED,
            selected=("example-private",),
        ),
    )
    _, subject = _verdict(groups)

    # The setting still fails (group_setting), but no public repository can see this group.
    assert subject is EnumReadingVerdict.WORKING


def test_public_sees_lab_is_working_when_the_group_refuses_public_repositories() -> (
    None
):
    groups = (lab_group(group_id=LAB_GROUP_ID, allows_public=False),)
    overall, subject = _verdict(groups)

    assert subject is EnumReadingVerdict.WORKING
    assert overall is EnumReadingVerdict.WORKING
