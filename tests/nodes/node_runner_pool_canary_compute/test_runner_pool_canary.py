# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22945 R0: a canary per rolled runner pool, and the rule that reads a pool.

Readback of rebuild step R0: a canary job completes on each rolled pool and its log shows the
timezone import exiting 0; a pool whose canary is missing or non-zero reads NOT WORKING, and a
pool's runners must read online in the organization listing to read WORKING.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_runner_pool_canary_compute.handlers.handler_runner_pool_canary import (
    HandlerRunnerPoolCanary,
    load_pool_rules,
    parse_canary_log,
)
from omnimarket.nodes.node_runner_pool_canary_compute.models.model_runner_pool_canary import (
    EnumRunnerPoolCanaryState,
    ModelRunnerPoolCanaryRequest,
    ModelRunnerPoolObservation,
)

pytestmark = pytest.mark.unit

GOOD_RUNNER = {
    "omninode-runner": "omninode-runner-3",
    "omninode-verify": "omninode-verify-runner-1",
    "customer-plane": "omninode-customer-plane-runner-2",
    "hp051-ci": "hp051-ci-runner-1",
    "hp051-ci-mq": "hp051-ci-mq-runner-2",
    "hp053-ci": "hp053-ci-runner-4",
    "mini": "omninode-mini-runner-1",
}
ALL_ONLINE = dict.fromkeys(GOOD_RUNNER, 2)


def _obs(pool: str, *, runner: str | None = None, exit_code: int | None = 0):
    return ModelRunnerPoolObservation(
        pool=pool,
        job_conclusion="success" if exit_code == 0 else "failure",
        runner_name=runner or GOOD_RUNNER[pool],
        exit_code=exit_code,
    )


def _read(observations, online=None, registered=None):
    request = ModelRunnerPoolCanaryRequest(
        correlation_id="r0-test",
        observations=tuple(observations),
        online_by_pool=ALL_ONLINE if online is None else online,
        registered_by_pool=ALL_ONLINE if registered is None else registered,
    )
    return HandlerRunnerPoolCanary().handle(request)


def _states(result):
    return {v.pool: v.state for v in result.verdicts}


def test_declares_the_seven_rolled_pools() -> None:
    rules = load_pool_rules()
    assert [r.pool for r in rules] == list(GOOD_RUNNER)


def test_zoneinfo_per_pool_all_seven_working() -> None:
    result = _read([_obs(p) for p in GOOD_RUNNER])
    assert result.all_working is True
    assert set(_states(result).values()) == {EnumRunnerPoolCanaryState.WORKING}
    assert len(result.verdicts) == 7


@pytest.mark.parametrize("pool", list(GOOD_RUNNER))
def test_zoneinfo_per_pool_failing_control_reads_not_working(pool: str) -> None:
    """Failing control: an image with no tzdata makes the import exit 1 on that pool."""
    obs = [_obs(p) for p in GOOD_RUNNER if p != pool] + [_obs(pool, exit_code=1)]
    states = _states(_read(obs))
    assert states[pool] is EnumRunnerPoolCanaryState.NOT_WORKING
    assert [
        p for p, s in states.items() if s is not EnumRunnerPoolCanaryState.WORKING
    ] == [pool]


def test_zoneinfo_per_pool_log_parse_positive_and_zoneinfo_error_negative() -> None:
    good = parse_canary_log(
        "CANARY_RESULT pool=hp053-ci runner=hp053-ci-runner-4 exit=0\n"
    )
    assert (good.pool, good.runner_name, good.exit_code) == (
        "hp053-ci",
        "hp053-ci-runner-4",
        0,
    )
    bad = parse_canary_log(
        "zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key America/New_York'\n"
        "CANARY_RESULT pool=hp053-ci runner=hp053-ci-runner-4 exit=1\n"
    )
    assert bad.exit_code == 1
    result = _read([_obs(p) for p in GOOD_RUNNER if p != "hp053-ci"] + [bad])
    assert _states(result)["hp053-ci"] is EnumRunnerPoolCanaryState.NOT_WORKING


def test_zoneinfo_per_pool_log_without_marker_is_not_an_exit_zero() -> None:
    assert parse_canary_log("some unrelated runner output\n").exit_code is None


def test_missing_pool_fails() -> None:
    result = _read([_obs(p) for p in GOOD_RUNNER if p != "customer-plane"])
    missing = next(v for v in result.verdicts if v.pool == "customer-plane")
    assert missing.state is EnumRunnerPoolCanaryState.NOT_WORKING
    assert "canary missing" in missing.reason
    assert result.all_working is False


def test_missing_pool_fails_when_job_ran_on_another_pools_runner() -> None:
    """A shared label set can land a job on a neighbour: that proves the neighbour only."""
    obs = [_obs(p) for p in GOOD_RUNNER if p != "omninode-runner"]
    obs.append(_obs("omninode-runner", runner="hp053-ci-runner-1"))
    assert (
        _states(_read(obs))["omninode-runner"] is EnumRunnerPoolCanaryState.NOT_WORKING
    )


def test_missing_pool_fails_when_runners_not_online() -> None:
    online = {**ALL_ONLINE, "hp051-ci-mq": 0}
    verdict = next(
        v
        for v in _read([_obs(p) for p in GOOD_RUNNER], online=online).verdicts
        if v.pool == "hp051-ci-mq"
    )
    assert verdict.state is EnumRunnerPoolCanaryState.NOT_WORKING
    assert "online" in verdict.reason


def test_missing_pool_fails_when_listing_has_no_row_for_pool() -> None:
    result = _read([_obs(p) for p in GOOD_RUNNER], online={}, registered={})
    assert set(_states(result).values()) == {EnumRunnerPoolCanaryState.NOT_WORKING}


def test_missing_pool_fails_when_no_canary_at_all() -> None:
    result = _read([])
    assert result.all_working is False
    assert len(result.verdicts) == 7
