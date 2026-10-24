# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction build waits for the session barrier (OMN-21113, TA.11 AC2)."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

import pytest
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    EnumContextCompactionTrigger,
    ModelContextCompactionRequested,
    ModelContextCompactionResult,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrier,
    ModelSessionStreamBarrierProbe,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_session_barrier_wait import (
    HandlerSessionBarrierWait,
)

pytestmark = pytest.mark.unit

INVOCATION = UUID("11111111-1111-4111-8111-111111111111")
BARRIER = "barrier-1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(barrier_id: str | None = BARRIER) -> ModelContextCompactionRequested:
    return ModelContextCompactionRequested(
        invocation_id=INVOCATION,
        session_id="session-1",
        cwd="/work/repo",
        trigger=EnumContextCompactionTrigger.MANUAL,
        input_messages_hash=_sha("input"),
        newest_turn="turn-9",
        barrier_id=barrier_id,
    )


def _probe(
    lineage_applied: tuple[int, ...],
    content_applied: tuple[int, ...],
    *,
    content_capture: Literal["enabled", "disabled"] | None = None,
) -> ModelSessionStreamBarrierProbe:
    return ModelSessionStreamBarrierProbe(
        session_id="session-1",
        barrier_id=BARRIER,
        source_revision="2026-10-23T12:00:00Z",
        barriers=(
            ModelSessionStreamBarrier(
                stream="lineage",
                barrier_id=BARRIER,
                high_water=3,
                content_capture=content_capture,
            ),
            ModelSessionStreamBarrier(
                stream="content", barrier_id=BARRIER, high_water=3
            ),
        ),
        applied={"lineage": lineage_applied, "content": content_applied},
    )


class _Clock:
    """A fake clock: sleeping advances it, so a deadline is reached without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Source:
    """Serves one probe per read; the last one repeats once the script is spent."""

    def __init__(self, probes: list[ModelSessionStreamBarrierProbe]) -> None:
        self.probes = probes
        self.reads: list[tuple[str, str]] = []

    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        self.reads.append((session_id, barrier_id))
        return self.probes[min(len(self.reads) - 1, len(self.probes) - 1)]


class _RaisingSource:
    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        raise RuntimeError("projection unreachable")


class _Build:
    def __init__(self):
        self.calls = 0

    def handle(self, command):
        from .test_compaction_pack_build import _ok_built

        self.calls += 1
        return _ok_built()


def _wait(
    source: _Source | _RaisingSource, clock: _Clock, deadline_s: float = 5.0
) -> HandlerSessionBarrierWait:
    return HandlerSessionBarrierWait(
        source,
        deadline_s=deadline_s,
        poll_interval_s=1.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def _orchestrate(
    compaction_bus,
    probes: list[ModelSessionStreamBarrierProbe],
    request: ModelContextCompactionRequested,
) -> tuple[ModelContextCompactionResult, _Build, _Source]:
    source = _Source(probes)
    build = _Build()
    handler = compaction_bus(build).handler(
        barrier_wait=_wait(source, _Clock()), barrier_probe_source=source
    )
    return handler.handle(request), build, source


def _fell_back(
    result: ModelContextCompactionResult,
) -> EnumContextCompactionFailureClass:
    assert result.reply.action == EnumClientVerdictAction.NATIVE
    assert result.reply.messages == ()
    assert result.prepared is None
    assert result.activated is None
    assert result.fallback is not None
    assert result.fallback.invocation_id == INVOCATION
    return result.fallback.failure_class


def test_a_barrier_not_met_at_the_end_of_the_wait_replies_native_source_not_caught_up(
    compaction_bus,
) -> None:
    result, build, source = _orchestrate(
        compaction_bus, [_probe((1, 2), (1, 2, 3))], _request()
    )
    assert _fell_back(result) == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    assert build.calls == 0
    assert len(source.reads) > 1


def test_a_delayed_tool_result_is_not_built_until_it_is_projected(
    compaction_bus,
) -> None:
    # The newest prompt (3) is projected while an earlier tool result (2) is
    # journalled but not projected: the build runs only on the read that closes
    # the gap.
    probes = [
        _probe((1, 3), (1, 2, 3)),
        _probe((1, 3), (1, 2, 3)),
        _probe((1, 2, 3), (1, 2, 3)),
    ]
    result, build, source = _orchestrate(compaction_bus, probes, _request())
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert build.calls == 1
    assert len(source.reads) == 4


def test_records_in_reverse_order_and_a_duplicate_do_not_delay_the_build(
    compaction_bus,
) -> None:
    result, build, source = _orchestrate(
        compaction_bus, [_probe((3, 2, 1, 2), (3, 3, 1, 2))], _request()
    )
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert build.calls == 1
    assert len(source.reads) == 2


def test_no_package_is_built_while_any_stream_is_below_its_high_water(
    compaction_bus,
) -> None:
    result, build, _ = _orchestrate(
        compaction_bus, [_probe((1, 2, 3), (1, 2))], _request()
    )
    assert _fell_back(result) == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    assert build.calls == 0


def test_a_barrier_not_yet_projected_replies_native_source_not_caught_up(
    compaction_bus,
) -> None:
    unprojected = ModelSessionStreamBarrierProbe(
        session_id="session-1",
        barrier_id=BARRIER,
        barriers=(),
        applied={"lineage": (1, 2, 3), "content": (1, 2, 3)},
    )
    result, build, _ = _orchestrate(compaction_bus, [unprojected], _request())
    assert _fell_back(result) == EnumContextCompactionFailureClass.SOURCE_NOT_CAUGHT_UP
    assert build.calls == 0


def test_a_session_with_content_capture_off_replies_native_source_disabled(
    compaction_bus,
) -> None:
    result, build, _ = _orchestrate(
        compaction_bus, [_probe((1, 2, 3), (), content_capture="disabled")], _request()
    )
    assert _fell_back(result) == EnumContextCompactionFailureClass.SOURCE_DISABLED
    assert build.calls == 0


def test_a_command_with_no_barrier_cannot_prove_completeness(compaction_bus) -> None:
    result, build, source = _orchestrate(
        compaction_bus, [_probe((1, 2, 3), (1, 2, 3))], _request(None)
    )
    assert _fell_back(result) == EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
    assert build.calls == 0
    assert source.reads == []


def test_a_failing_barrier_read_replies_native_build_error(compaction_bus) -> None:
    build = _Build()
    handler = compaction_bus(build).handler(
        barrier_wait=_wait(_RaisingSource(), _Clock())
    )
    assert (
        _fell_back(handler.handle(_request()))
        == EnumContextCompactionFailureClass.BUILD_ERROR
    )
    assert build.calls == 0


def test_without_a_barrier_wait_the_build_runs_as_before(compaction_bus) -> None:
    build = _Build()
    result = compaction_bus(build).handler().handle(_request())
    assert result.reply.action == EnumClientVerdictAction.INSTALL
    assert build.calls == 1


@pytest.mark.parametrize(
    ("deadline_s", "reads"), [(0.0, 1), (2.5, 4)], ids=["spent", "2.5s"]
)
def test_the_wait_stops_at_its_deadline(deadline_s: float, reads: int) -> None:
    clock = _Clock()
    source = _Source([_probe((1,), (1,))])
    verdict = _wait(source, clock, deadline_s).wait(_request())
    assert verdict.outcome == "not_met"
    assert len(source.reads) == reads
    assert clock.now >= deadline_s


def test_the_wait_reads_the_commands_session_and_barrier() -> None:
    source = _Source([_probe((1, 2, 3), (1, 2, 3))])
    verdict = _wait(source, _Clock()).wait(_request())
    assert verdict.outcome == "met"
    assert source.reads == [("session-1", BARRIER)]
