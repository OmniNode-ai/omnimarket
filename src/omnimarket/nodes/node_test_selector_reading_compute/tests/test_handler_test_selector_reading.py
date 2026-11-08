# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test selector reading (OMN-22951): activation read-back and shadow misses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.test_selector_reading import (
    SELECTOR_REPOSITORIES,
    EnumSelectorActivation,
    EnumSelectorReadingState,
    ModelSelectorRepoObservation,
    ModelSelectorShadowComparison,
    ModelTestSelectorReading,
    ModelTestSelectorReadingRequest,
)
from omnimarket.nodes.node_test_selector_reading_compute.handlers import (
    HandlerTestSelectorReading,
)

NOW = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
WORKFLOW_ON = (
    "env:\n  FLAG: ${{ vars.ENABLE_SMART_TESTS == 'true' && 'on' || 'off' }}\n"
)
WORKFLOW_NO_FLAG = "env:\n  FLAG: 'off'\n"
SHA = "a" * 40
OTHER = "b" * 40


def _obs(
    repo: str,
    *,
    workflow: str | None = WORKFLOW_ON,
    flag: str | None = "true",
    flag_readable: bool = True,
    contexts: tuple[str, ...] | None = ("CI Summary", "Detect Changes"),
    comparisons: tuple[ModelSelectorShadowComparison, ...] = (),
) -> ModelSelectorRepoObservation:
    return ModelSelectorRepoObservation(
        repo=repo,
        workflow_text=workflow,
        flag_value=flag,
        flag_readable=flag_readable,
        required_contexts=contexts,
        comparisons=comparisons,
    )


def _read(
    *obs: ModelSelectorRepoObservation, expected: tuple[str, ...] | None = None
) -> ModelTestSelectorReading:
    return HandlerTestSelectorReading().handle(
        ModelTestSelectorReadingRequest(
            observations=obs,
            expected_repos=expected or tuple(o.repo for o in obs),
            now=NOW,
        )
    )


def _all_active() -> tuple[ModelSelectorRepoObservation, ...]:
    return tuple(_obs(r) for r in SELECTOR_REPOSITORIES)


def test_activation_readback_all_four_active_reads_working() -> None:
    reading = HandlerTestSelectorReading().handle(
        ModelTestSelectorReadingRequest(observations=_all_active(), now=NOW)
    )
    assert reading.state is EnumSelectorReadingState.WORKING
    assert reading.shadow_misses == 0
    assert [r.repo for r in reading.repos] == list(SELECTOR_REPOSITORIES)
    assert all(r.activation is EnumSelectorActivation.ACTIVE for r in reading.repos)
    assert all(
        r.workflow_references_flag and r.flag_enabled and r.protection_requires_selector
        for r in reading.repos
    )


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"workflow": WORKFLOW_NO_FLAG}, "workflow does not reference"),
        ({"flag": None}, "is not true"),
        ({"flag": "false"}, "is not true"),
        ({"contexts": ("CI Summary",)}, "branch protection does not require"),
    ],
)
def test_activation_readback_each_source_alone_disables(
    override: dict[str, Any], why: str
) -> None:
    repo = SELECTOR_REPOSITORIES[0]
    reading = _read(_obs(repo, **override))
    assert reading.repos[0].activation is EnumSelectorActivation.DISABLED
    assert why in reading.repos[0].reason
    assert reading.state is EnumSelectorReadingState.NOT_WORKING


def test_activation_readback_unreadable_source_reads_unknown() -> None:
    for override in (
        {"workflow": None},
        {"flag_readable": False, "flag": None},
        {"contexts": None},
    ):
        reading = _read(_obs("omnimarket", **override))
        assert reading.repos[0].activation is EnumSelectorActivation.UNREADABLE
        assert reading.state is EnumSelectorReadingState.UNKNOWN
        assert reading.shadow_misses == 0


def test_activation_readback_missing_repository_reads_unknown() -> None:
    reading = HandlerTestSelectorReading().handle(
        ModelTestSelectorReadingRequest(observations=_all_active()[:3], now=NOW)
    )
    assert reading.state is EnumSelectorReadingState.UNKNOWN
    assert reading.repos[3].repo == SELECTOR_REPOSITORIES[3]


def test_shadow_miss_disabled_selector_is_non_zero_and_red() -> None:
    reading = _read(_obs("omnimarket", flag=None))
    assert reading.shadow_misses == 1
    assert reading.state is EnumSelectorReadingState.NOT_WORKING
    assert reading.repos[0].missed_failures == ()


def test_shadow_miss_failure_missed_on_same_head_is_counted_and_red() -> None:
    cmp = ModelSelectorShadowComparison(
        pr="omnimarket#1",
        selected_head_sha=SHA,
        full_suite_head_sha=SHA,
        selected_failures=("t::a",),
        full_suite_failures=("t::a", "t::b", "t::c"),
    )
    reading = _read(_obs("omnimarket", comparisons=(cmp,)))
    assert reading.shadow_misses == 2
    assert reading.repos[0].missed_failures == (
        "omnimarket#1::t::b",
        "omnimarket#1::t::c",
    )
    assert reading.repos[0].matched_comparisons == 1
    assert reading.state is EnumSelectorReadingState.NOT_WORKING


def test_shadow_miss_positive_control_caught_failures_read_zero() -> None:
    cmp = ModelSelectorShadowComparison(
        pr="omnimarket#1",
        selected_head_sha=SHA,
        full_suite_head_sha=SHA,
        selected_failures=("t::a", "t::extra"),
        full_suite_failures=("t::a",),
    )
    reading = _read(_obs("omnimarket", comparisons=(cmp,)))
    assert reading.shadow_misses == 0
    assert reading.state is EnumSelectorReadingState.WORKING


def test_shadow_miss_other_head_comparison_is_not_counted_as_a_miss() -> None:
    cmp = ModelSelectorShadowComparison(
        pr="omnimarket#1",
        selected_head_sha=SHA,
        full_suite_head_sha=OTHER,
        full_suite_failures=("t::b",),
    )
    reading = _read(_obs("omnimarket", comparisons=(cmp,)))
    assert reading.shadow_misses == 0
    assert reading.repos[0].unmatched_comparisons == 1
    assert reading.repos[0].matched_comparisons == 0


def test_shadow_miss_total_sums_per_repository() -> None:
    cmp = ModelSelectorShadowComparison(
        pr="p#1",
        selected_head_sha=SHA,
        full_suite_head_sha=SHA,
        full_suite_failures=("x",),
    )
    reading = _read(
        _obs("omnibase_core", comparisons=(cmp,)),
        _obs("omnimarket", flag="false"),
        _obs("omniclaude"),
    )
    assert {r.repo: r.shadow_misses for r in reading.repos} == {
        "omnibase_core": 1,
        "omnimarket": 1,
        "omniclaude": 0,
    }
    assert reading.shadow_misses == 2
