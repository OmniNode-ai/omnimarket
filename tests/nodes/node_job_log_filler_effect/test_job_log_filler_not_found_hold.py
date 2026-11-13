# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A job whose log the gateway answers HTTP 404 is held, so live reads go to jobs that can fill (OMN-19998).

Measured 2026-11-12 on the operator's Mac: about 20 fills per tick failed with
``GatewayReadError: gateway job_log_tail failed: job log not cached: HTTP 404``. The handler's held memo
remembered only filled or cached jobs, so the same 404 jobs were read live again on every tick and took 20 of
the 30 live reads, leaving 0 to 3 new fills. One cause is measured: ``lab-proof-receipt`` is a check run posted
by a GitHub App (its check-run id is not an Actions job id), so ``actions/jobs/<id>/logs`` has no log for it
and answers 404 for as long as the run exists. These tests pin the repair:

* the failing control: with no hold setting the same 404 job takes the read cap on every pass;
* with the hold the 404 job is read once per interval, recorded with its cause, and the cap goes to a job that
  can fill;
* the hold ends on its own (interval passed, or the job left the open pull requests).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult
from omnimarket.nodes.node_job_log_filler_effect import (
    runtime_job_log_filler_tick as tick,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers import adapter_job_log_readers
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    BusJobLogReader,
    is_log_not_found,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
    ModelJobLogHold,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
    JobLogNotFoundError,
)
from tests.nodes.node_job_log_filler_effect.planted import _record, overlay

pytestmark = pytest.mark.unit

GONE_MESSAGE = "job log not cached: HTTP 404, read_ceiling_hit=False"
GONE_JOB = 128000002000  # the newest completion, so it is first in the pass order
FILLABLE_JOB = 128000001000
HOLD_S = 3600


class GoneReader:
    """Every log is missing until filled; the logs of ``gone`` jobs answer HTTP 404 on a live read."""

    cache_path: str | None = None

    def __init__(self) -> None:
        self.cached: set[int] = set()
        self.gone: set[int] = {GONE_JOB}
        self.fills: list[int] = []

    def read(
        self,
        repo: str,
        job_id: int,
        *,
        cache_only: bool,
        tail_bytes: int | None = None,
    ) -> ModelJobLogTailResult:
        if cache_only:
            if job_id not in self.cached:
                raise JobLogCacheMissError(f"JOB-LOG-CACHE-MISS {job_id}")
        else:
            self.fills.append(job_id)
            if job_id in self.gone:
                raise JobLogNotFoundError(GONE_MESSAGE)
            self.cached.add(job_id)
        return ModelJobLogTailResult(
            repo=repo, job_id=job_id, total_bytes=10, truncated=False, text="x"
        )


def state(*jobs: int) -> dict[str, Any]:
    """One red pull request per job, the later job id completing later."""
    prs: dict[str, Any] = {}
    for number, job in enumerate(sorted(jobs), start=1):
        prs[f"omnimarket#{number}"] = _record(
            number,
            "red",
            red=["Tests Gate"],
            detail=[["Tests Gate", str(job), f"2026-11-12T08:{number:02d}:00Z"]],
        )
    return {"last_tick": "2026-11-12T09:00:00Z", "prs": prs}


def build(
    hold: int | None, clock: list[float], watcher: dict[str, Any] | None = None
) -> tuple[HandlerJobLogFiller, GoneReader]:
    reader = GoneReader()
    handler = HandlerJobLogFiller(
        overlay=overlay(max_reads_per_pass=1, not_found_hold_seconds=hold),
        reader_factory=lambda _profile: reader,
        state_reader=lambda: watcher or state(GONE_JOB, FILLABLE_JOB),
        clock=lambda: clock[0],
    )
    return handler, reader


def test_a_404_job_is_not_read_again_inside_the_hold() -> None:
    clock = [1_000_000.0]
    handler, reader = build(HOLD_S, clock)
    first = handler.fill(ModelJobLogFillRequest())
    assert reader.fills == [GONE_JOB]
    assert first.failed == 1
    assert "HTTP 404" in handler.not_found[GONE_JOB].cause
    second = handler.fill(ModelJobLogFillRequest())
    # the read goes to the job that can fill; the 404 job is parked, with its cause
    assert reader.fills == [GONE_JOB, FILLABLE_JOB]
    assert second.filled == 1
    assert second.failed == 0
    assert second.held_not_found == 1
    parked = next(i for i in second.items if i.run.job_id == GONE_JOB)
    assert parked.outcome is EnumJobLogFillOutcome.HELD_NOT_FOUND
    assert parked.error is not None
    assert "HTTP 404" in parked.error


def test_without_the_hold_the_404_job_takes_the_read_every_pass() -> None:
    """The failing control: no hold setting, the measured fault."""
    clock = [1_000_000.0]
    handler, reader = build(None, clock)
    handler.fill(ModelJobLogFillRequest())
    second = handler.fill(ModelJobLogFillRequest())
    assert reader.fills == [GONE_JOB, GONE_JOB]
    assert FILLABLE_JOB not in reader.cached
    assert second.failed == 1
    assert second.held_not_found == 0
    assert handler.not_found == {}


def test_the_404_job_is_read_again_after_the_hold_interval() -> None:
    clock = [1_000_000.0]
    handler, reader = build(HOLD_S, clock)
    handler.fill(ModelJobLogFillRequest())
    clock[0] += HOLD_S + 1
    third = handler.fill(ModelJobLogFillRequest())
    assert reader.fills == [GONE_JOB, GONE_JOB]
    assert third.held_not_found == 0
    assert third.failed == 1


def test_a_job_that_leaves_the_open_pull_requests_is_dropped_from_the_memo() -> None:
    clock = [1_000_000.0]
    watcher = state(GONE_JOB, FILLABLE_JOB)
    handler, _ = build(HOLD_S, clock, watcher)
    handler.fill(ModelJobLogFillRequest())
    assert GONE_JOB in handler.not_found
    watcher["prs"].pop("omnimarket#2")  # the pull request of the 404 job is gone
    handler.fill(ModelJobLogFillRequest())
    assert handler.not_found == {}


def test_the_first_404_counts_failed_once_then_the_pass_completes() -> None:
    clock = [1_000_000.0]
    handler, _ = build(HOLD_S, clock)
    first = handler.fill(ModelJobLogFillRequest())
    assert first.status is EnumJobLogFillStatus.FAILED
    second = handler.fill(ModelJobLogFillRequest())
    assert second.status is EnumJobLogFillStatus.COMPLETED
    assert second.held_not_found == 1


def test_a_held_job_is_not_remembered_as_cached() -> None:
    clock = [1_000_000.0]
    handler, _ = build(HOLD_S, clock)
    handler.fill(ModelJobLogFillRequest())
    handler.fill(ModelJobLogFillRequest())
    assert GONE_JOB not in handler.held
    assert FILLABLE_JOB in handler.held


def test_the_adapter_maps_a_gateway_404_to_the_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = f"gateway job_log_tail failed: {GONE_MESSAGE}"
    assert is_log_not_found(text) is True
    assert is_log_not_found("job log not cached: HTTP 500") is False

    def gone(*_: object, **__: object) -> object:
        raise GatewayReadError(text)

    def broken(*_: object, **__: object) -> object:
        raise GatewayReadError(
            "gateway job_log_tail failed: job log not cached: HTTP 500"
        )

    reader = BusJobLogReader(lane="dev", timeout_seconds=1.0, tail_bytes=10)
    monkeypatch.setattr(adapter_job_log_readers, "read_over_bus", gone)
    with pytest.raises(JobLogNotFoundError):
        reader.read("OmniNode-ai/omnimarket", 1, cache_only=False)
    monkeypatch.setattr(adapter_job_log_readers, "read_over_bus", broken)
    with pytest.raises(GatewayReadError) as raised:
        reader.read("OmniNode-ai/omnimarket", 1, cache_only=False)
    assert not isinstance(raised.value, JobLogNotFoundError)


def test_the_tick_keeps_the_404_memo_between_ticks(tmp_path: Path) -> None:
    memo = tmp_path / tick.NOT_FOUND_FILE
    memo.write_text(
        json.dumps({"123": {"held_at": 5.0, "cause": "x"}}), encoding="utf-8"
    )
    assert tick.read_not_found(memo) == {123: ModelJobLogHold(held_at=5.0, cause="x")}
    assert tick.read_not_found(tmp_path / "absent.json") == {}
    memo.write_text("not json", encoding="utf-8")
    assert tick.read_not_found(memo) == {}


def test_a_second_tick_reads_the_memo_the_first_tick_wrote(tmp_path: Path) -> None:
    """The seam between ticks: the memo file one tick writes is the one the next tick holds the job by."""
    clock = [1_000_000.0]
    first, first_reader = build(HOLD_S, clock)
    argv = ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    assert tick.main(argv, handler=first) == tick.EXIT_FAILED
    written = json.loads((tmp_path / "state" / tick.NOT_FOUND_FILE).read_text())
    assert "HTTP 404" in written[str(GONE_JOB)]["cause"]
    reader = GoneReader()
    reader.gone = {GONE_JOB}
    second = HandlerJobLogFiller(
        overlay=overlay(max_reads_per_pass=1, not_found_hold_seconds=HOLD_S),
        reader_factory=lambda _profile: reader,
        state_reader=lambda: state(GONE_JOB, FILLABLE_JOB),
        clock=lambda: clock[0],
        not_found=tick.read_not_found(tmp_path / "state" / tick.NOT_FOUND_FILE),
    )
    assert tick.main(argv, handler=second) == 0
    assert reader.fills == [FILLABLE_JOB]
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert evidence["held_not_found"] == 1
    assert first_reader.fills == [GONE_JOB]
