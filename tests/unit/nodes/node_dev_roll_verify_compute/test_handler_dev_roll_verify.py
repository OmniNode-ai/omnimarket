# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Table tests of the start-count verdict, every rule in the order the handler checks them."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_verify_compute.handlers import (
    HandlerDevRollVerify,
    decide_dev_roll_verify,
)
from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerdict,
    EnumDevRollVerifyRefusal,
    EnumDevStartCause,
    ModelDevRollVerified,
    ModelDevRollVerifyPolicy,
    ModelDevRollVerifyRefused,
)
from omnimarket.nodes.node_dev_roll_verify_compute.overlay import (
    load_dev_roll_verify_policy,
)
from tests.unit.nodes.node_dev_roll_verify_compute.conftest import (
    DECLARED_INSTALLS,
    NOW,
    POLICY_DOC,
    facts,
    start,
    write_overlay,
)

pytestmark = pytest.mark.unit


def _verified(result: object) -> ModelDevRollVerified:
    assert isinstance(result, ModelDevRollVerified)
    return result


def _refused(result: object) -> ModelDevRollVerifyRefused:
    assert isinstance(result, ModelDevRollVerifyRefused)
    return result


def test_one_roll_recreate_start_passes(policy: ModelDevRollVerifyPolicy) -> None:
    d = _verified(
        decide_dev_roll_verify(facts((start(seconds_after_roll_start=60),)), policy)
    )
    assert d.verdict is EnumDevRollVerdict.PASSED
    assert d.starts_counted == 1
    assert d.declared_starts == 1
    assert d.reapply_starts == 0
    assert d.starts_by_cause == {"roll_recreate": 1}
    assert d.declared_branch_installs == DECLARED_INSTALLS
    expected = NOW - timedelta(seconds=7200) + timedelta(seconds=60)
    assert d.first_start_at == expected
    assert d.last_start_at == expected


def test_two_starts_with_a_reapply_verdict_too_many_starts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    starts = (
        start(seconds_after_roll_start=60, cause=EnumDevStartCause.ROLL_RECREATE),
        start(
            seconds_after_roll_start=120,
            cause=EnumDevStartCause.BRANCH_INSTALL_REAPPLY,
        ),
    )
    d = _verified(decide_dev_roll_verify(facts(starts), policy))
    assert d.verdict is EnumDevRollVerdict.TOO_MANY_STARTS
    assert d.starts_counted == 2
    assert d.declared_starts == 1
    assert d.reapply_starts == 1
    assert d.starts_by_cause == {"branch_install_reapply": 1, "roll_recreate": 1}
    assert d.first_start_at < d.last_start_at


def test_zero_starts_verdicts_no_start(policy: ModelDevRollVerifyPolicy) -> None:
    d = _verified(decide_dev_roll_verify(facts(()), policy))
    assert d.verdict is EnumDevRollVerdict.NO_START
    assert d.starts_counted == 0
    assert d.reapply_starts == 0
    assert d.starts_by_cause == {}
    assert d.first_start_at is None
    assert d.last_start_at is None


def test_unreadable_starts_verdict_start_count_unknown(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    d = _verified(decide_dev_roll_verify(facts(None), policy))
    assert d.verdict is EnumDevRollVerdict.START_COUNT_UNKNOWN
    assert d.starts_counted is None
    assert d.declared_starts == 1
    assert d.first_start_at is None
    assert d.last_start_at is None


def test_a_declared_bound_of_two_passes_two_starts(tmp_path: Path) -> None:
    two = load_dev_roll_verify_policy(
        write_overlay(tmp_path, {**POLICY_DOC, "declared_starts_per_roll": 2})
    )
    starts = tuple(start(seconds_after_roll_start=60 * n) for n in (1, 2))
    d = _verified(decide_dev_roll_verify(facts(starts), two))
    assert d.verdict is EnumDevRollVerdict.PASSED
    assert d.starts_counted == 2
    assert d.declared_starts == 2
    assert d.reapply_starts == 0


def test_start_order_does_not_change_the_result(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    starts = [start(seconds_after_roll_start=60 + 37 * n) for n in range(4)]
    base = facts(tuple(starts))
    expected = decide_dev_roll_verify(base, policy)
    rng = random.Random(11)
    for _ in range(5):
        rng.shuffle(starts)
        shuffled = base.model_copy(update={"starts": tuple(starts)})
        assert decide_dev_roll_verify(shuffled, policy) == expected


def test_control_today_two_starts_per_roll_fails(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    """Two starts per roll violate the declared one-start rule, not the two-start one."""
    starts = (
        start(seconds_after_roll_start=60, cause=EnumDevStartCause.ROLL_RECREATE),
        start(
            seconds_after_roll_start=120,
            cause=EnumDevStartCause.BRANCH_INSTALL_REAPPLY,
        ),
    )
    declared_one = _verified(decide_dev_roll_verify(facts(starts), policy))
    assert declared_one.verdict is not EnumDevRollVerdict.PASSED
    assert declared_one.verdict is EnumDevRollVerdict.TOO_MANY_STARTS
    declared_two = _verified(
        decide_dev_roll_verify(
            facts(starts),
            ModelDevRollVerifyPolicy(declared_starts_per_roll=2, runtime_lane="dev"),
        )
    )
    assert declared_two.verdict is EnumDevRollVerdict.PASSED


def test_an_unfinished_roll_is_refused(policy: ModelDevRollVerifyPolicy) -> None:
    d = _refused(
        decide_dev_roll_verify(facts((start(),), roll_finished_at=None), policy)
    )
    assert d.reason is EnumDevRollVerifyRefusal.ROLL_NOT_FINISHED
    assert d.job_id == "job-1"


def test_a_roll_finishing_after_observed_at_is_refused(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    d = _refused(
        decide_dev_roll_verify(
            facts((start(),), roll_finished_at=NOW + timedelta(seconds=100)), policy
        )
    )
    assert d.reason is EnumDevRollVerifyRefusal.ROLL_NOT_FINISHED


def test_a_start_for_another_roll_refuses_the_facts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    d = _refused(decide_dev_roll_verify(facts((start(job_id="job-2"),)), policy))
    assert d.reason is EnumDevRollVerifyRefusal.START_FOR_OTHER_ROLL
    assert d.job_id == "job-1"


def test_a_start_after_observed_at_refuses_the_facts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    d = _refused(
        decide_dev_roll_verify(facts((start(seconds_after_roll_start=8000),)), policy)
    )
    assert d.reason is EnumDevRollVerifyRefusal.START_AFTER_OBSERVED_AT


def test_a_repeated_start_event_refuses_the_facts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    event_id = uuid4()
    d = _refused(
        decide_dev_roll_verify(
            facts(
                (
                    start(seconds_after_roll_start=60, event_id=event_id),
                    start(seconds_after_roll_start=120, event_id=event_id),
                )
            ),
            policy,
        )
    )
    assert d.reason is EnumDevRollVerifyRefusal.DUPLICATE_START_EVENT


async def test_the_handler_class_answers_with_the_same_verdict(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    f = facts((start(seconds_after_roll_start=60),))
    assert await HandlerDevRollVerify(policy).handle(f) == decide_dev_roll_verify(
        f, policy
    )
