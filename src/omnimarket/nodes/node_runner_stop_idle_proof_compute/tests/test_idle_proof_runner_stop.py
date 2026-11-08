# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle proof prints WORKING only on two idle reads 60 seconds apart (OMN-22952)."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers import (
    HandlerRunnerStopIdleProof,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers.handler_runner_stop_idle_proof import (
    group_idle_subject,
    group_read_subject,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    EnumIdleProofReadFailure,
    ModelProofRunner,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.tests.support import (
    GAP_SECONDS,
    GROUP_A,
    GROUP_B,
    idle_read,
    request,
)

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


def _subject(result: object, name: str) -> EnumReadingVerdict:
    for s in result.subjects:  # type: ignore[attr-defined]
        if s.name == name:
            return s.verdict  # type: ignore[no-any-return]
    raise AssertionError(f"no subject {name}")


def test_idle_proof_two_idle_reads_sixty_seconds_apart_reads_working() -> None:
    result = HandlerRunnerStopIdleProof().handle(request())
    assert result.verdict is _W
    assert result.reads_judged == 2
    assert result.busy_runners == ()
    assert result.blocking_job_ids == ()
    assert _subject(result, group_idle_subject(GROUP_A)) is _W
    assert _subject(result, group_idle_subject(GROUP_B)) is _W


def test_idle_proof_busy_runner_in_group_three_reads_not_working() -> None:
    busy = idle_read(
        0,
        runners=(ModelProofRunner(name="busy-a", group_id=GROUP_A, busy=True),),
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((busy, idle_read(GAP_SECONDS)))
    )
    assert result.verdict is _N
    assert result.busy_runners == ("busy-a",)
    assert _subject(result, group_idle_subject(GROUP_A)) is _N


def test_idle_proof_busy_runner_in_group_seven_on_second_read_reads_not_working() -> (
    None
):
    busy = idle_read(
        GAP_SECONDS,
        runners=(ModelProofRunner(name="busy-b", group_id=GROUP_B, busy=True),),
    )
    result = HandlerRunnerStopIdleProof().handle(request((idle_read(0), busy)))
    assert result.verdict is _N
    assert result.busy_runners == ("busy-b",)
    assert _subject(result, group_idle_subject(GROUP_B)) is _N


def test_idle_proof_busy_runner_outside_the_proof_groups_does_not_block() -> None:
    result = HandlerRunnerStopIdleProof().handle(request())
    assert result.busy_runners == ()
    assert result.verdict is _W


def test_idle_proof_one_read_reads_unknown() -> None:
    result = HandlerRunnerStopIdleProof().handle(request((idle_read(GAP_SECONDS),)))
    assert result.verdict is _U


def test_idle_proof_no_reads_reads_unknown() -> None:
    assert HandlerRunnerStopIdleProof().handle(request(())).verdict is _U


def test_idle_proof_reads_closer_than_the_gap_read_unknown() -> None:
    result = HandlerRunnerStopIdleProof().handle(
        request((idle_read(0), idle_read(GAP_SECONDS - 1)))
    )
    assert result.verdict is _U
    assert _subject(result, "read_spacing") is _U


def test_idle_proof_reads_exactly_at_the_gap_read_working() -> None:
    result = HandlerRunnerStopIdleProof().handle(
        request((idle_read(0), idle_read(GAP_SECONDS)))
    )
    assert _subject(result, "read_spacing") is _W


def test_idle_proof_only_the_newest_reads_are_judged() -> None:
    old_busy = idle_read(
        -GAP_SECONDS,
        runners=(ModelProofRunner(name="old", group_id=GROUP_A, busy=True),),
    )
    result = HandlerRunnerStopIdleProof().handle(
        request((old_busy, idle_read(0), idle_read(GAP_SECONDS)))
    )
    assert result.verdict is _W
    assert result.reads_judged == 2


def test_idle_proof_stale_newest_read_reads_unknown() -> None:
    result = HandlerRunnerStopIdleProof().handle(request(now_offset_seconds=1000))
    assert result.verdict is _U
    assert _subject(result, "read_age") is _U


def test_idle_proof_newest_read_after_now_reads_unknown() -> None:
    result = HandlerRunnerStopIdleProof().handle(request(now_offset_seconds=10))
    assert result.verdict is _U


def test_idle_proof_failed_read_reads_unknown() -> None:
    failed = idle_read(
        GAP_SECONDS, read_failure=EnumIdleProofReadFailure.ORG_READ_DENIED
    )
    result = HandlerRunnerStopIdleProof().handle(request((idle_read(0), failed)))
    assert result.verdict is _U
    assert _subject(result, "read_failures") is _U


def test_idle_proof_unread_proof_group_reads_unknown() -> None:
    partial = idle_read(GAP_SECONDS, groups_read=(GROUP_A,))
    result = HandlerRunnerStopIdleProof().handle(request((idle_read(0), partial)))
    assert result.verdict is _U
    assert _subject(result, group_read_subject(GROUP_B)) is _U
    assert _subject(result, group_read_subject(GROUP_A)) is _W


def test_idle_proof_busy_outranks_unknown() -> None:
    busy = idle_read(
        0,
        runners=(ModelProofRunner(name="busy-a", group_id=GROUP_A, busy=True),),
    )
    result = HandlerRunnerStopIdleProof().handle(request((busy,)))
    assert result.verdict is _N
