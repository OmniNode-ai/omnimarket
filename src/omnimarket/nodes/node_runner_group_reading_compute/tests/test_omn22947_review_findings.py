# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of OMN-22947: sub-second staleness, stale canary runs and an omitted runner inventory."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    ModelCanaryRun,
    ModelGroupFact,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    LAB_GROUP_ID,
    canary,
    request,
    settings,
)

_HANDLER = HandlerRunnerGroupReading()
_LIMIT = settings().max_age_seconds


def _age_verdict(age_seconds: float) -> EnumReadingVerdict:
    result = _HANDLER.handle(request(age_seconds=age_seconds))  # type: ignore[arg-type]
    return next(s for s in result.subjects if s.name == "read_age").verdict


def test_a_read_half_a_second_past_the_limit_is_stale() -> None:
    assert _age_verdict(_LIMIT + 0.5) is EnumReadingVerdict.UNKNOWN


def test_a_read_dated_half_a_second_after_now_is_future_dated() -> None:
    assert _age_verdict(-0.5) is EnumReadingVerdict.UNKNOWN


def test_a_read_exactly_at_the_limit_is_still_fresh() -> None:
    assert _age_verdict(_LIMIT) is EnumReadingVerdict.WORKING


def test_a_stale_canary_does_not_name_the_group_it_reported() -> None:
    stale = canary(
        ModelCanaryRun(
            run_id=1, repository="example-public", picked_up_by_group_id=LAB_GROUP_ID
        ),
        age_seconds=_LIMIT + 100,
    )
    result = _HANDLER.handle(request(canary_facts=stale))
    assert result.failing_group_ids == ()


def test_a_fresh_canary_started_on_a_lab_group_names_it() -> None:
    fresh = canary(
        ModelCanaryRun(
            run_id=1, repository="example-public", picked_up_by_group_id=LAB_GROUP_ID
        )
    )
    result = _HANDLER.handle(request(canary_facts=fresh))
    assert result.failing_group_ids == (LAB_GROUP_ID,)


def test_a_group_with_no_runner_inventory_is_refused_not_read_as_empty() -> None:
    with pytest.raises(ValidationError, match="runners"):
        ModelGroupFact.model_validate(
            {
                "group_id": 1,
                "name": "group-1",
                "visibility": EnumGroupVisibility.ALL,
                "allows_public_repositories": False,
            }
        )


def test_a_group_with_an_explicitly_empty_inventory_is_accepted() -> None:
    group = ModelGroupFact(
        group_id=1,
        name="group-1",
        visibility=EnumGroupVisibility.ALL,
        allows_public_repositories=False,
        runners=(),
    )
    assert group.runners == ()
