# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The job-log filler (OMN-23032): selection, the pass, and the lane read it feeds.

The failing control: a merge-relevant run with no cached log makes the lane's ``job-log``
read refuse and name the cache path; after the filler runs, the same read answers from the
cache. The injected break is a reader that fetches nothing: the read still refuses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.handlers.scm.scm_cli import scm_group
from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult
from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
    handler_github_repo_gateway as gateway,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
    select_merge_relevant_runs,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    EnumJobLogFillReason,
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
)
from tests.nodes.node_job_log_filler_effect.planted import (
    GREEN_JOB,
    RED_JOB,
    RED_LOG,
    REPO,
    STALE_JOB,
    SUMMARY_JOB,
    overlay,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit


def test_selection_takes_red_and_named_jobs_of_the_current_head_only() -> None:
    runs, stale = select_merge_relevant_runs(watcher_state(), overlay())
    assert [(r.job_name, r.job_id, r.reason) for r in runs] == [
        (
            "Tests Gate",
            RED_JOB,
            EnumJobLogFillReason.RED,
        ),  # the later attempt, not 127999999999; red before named
        ("CI Summary", SUMMARY_JOB, EnumJobLogFillReason.NAMED),
    ]
    assert {r.repo for r in runs} == {REPO}
    assert stale == 1  # omnimarket#5's CI record is of another head
    assert GREEN_JOB not in {r.job_id for r in runs}
    assert STALE_JOB not in {r.job_id for r in runs}


def test_selection_follows_the_overlay_and_the_repo_narrowing() -> None:
    runs, _ = select_merge_relevant_runs(watcher_state(), overlay(always_job_names=()))
    assert [r.job_id for r in runs] == [RED_JOB]
    runs, _ = select_merge_relevant_runs(
        watcher_state(), overlay(), repos=("OmniNode-ai/elsewhere",)
    )
    assert runs == []
    runs, _ = select_merge_relevant_runs(
        watcher_state(), overlay(pr_classes=("pending",))
    )
    assert runs == []


def _handler(tmp_path: Path, transport, **settings) -> tuple[HandlerJobLogFiller, Path]:
    root = tmp_path / "job-logs"
    return (
        HandlerJobLogFiller(
            overlay=overlay(**settings),
            reader_factory=lambda _profile: InMemoryJobLogReader(
                root,
                ttl_seconds=86400,
                tail_bytes=4096,
                transport_factory=lambda: transport,
            ),
            state_reader=watcher_state,
        ),
        root,
    )


def test_a_pass_fills_each_missing_log_once_and_skips_cached_ones(
    tmp_path: Path,
) -> None:
    transport = transport_for(RED_JOB, SUMMARY_JOB)
    handler, root = _handler(tmp_path, transport)
    first = handler.fill(ModelJobLogFillRequest())
    assert first.status is EnumJobLogFillStatus.COMPLETED
    assert (first.considered, first.filled, first.already_cached, first.failed) == (
        2,
        2,
        0,
        0,
    )
    assert first.cache_path == str(root)
    assert len(transport.sent) == 2
    second = handler.fill(ModelJobLogFillRequest())
    assert (second.filled, second.already_cached) == (0, 2)
    assert len(transport.sent) == 2  # the second pass made no GitHub read
    transport.assert_drained()


def test_a_404_is_recorded_against_its_run_and_the_pass_goes_on(tmp_path: Path) -> None:
    transport = transport_for(
        RED_JOB, SUMMARY_JOB, bodies={SUMMARY_JOB: (404, b'{"message":"Not Found"}')}
    )
    handler, _ = _handler(tmp_path, transport, reader_concurrency=1)
    result = handler.fill(ModelJobLogFillRequest())
    assert result.status is EnumJobLogFillStatus.PARTIAL
    assert (result.filled, result.failed) == (1, 1)
    failed = next(i for i in result.items if i.outcome is EnumJobLogFillOutcome.FAILED)
    assert failed.run.job_id == SUMMARY_JOB
    assert "404" in (failed.error or "")


def test_the_cap_bounds_live_reads_and_defers_the_rest(tmp_path: Path) -> None:
    transport = transport_for(
        RED_JOB
    )  # the red job is first in line, the named one waits
    handler, _ = _handler(tmp_path, transport, max_reads_per_pass=1)
    result = handler.fill(ModelJobLogFillRequest())
    assert (result.filled, result.deferred) == (1, 1)
    assert len(transport.sent) == 1
    # a request can lower the cap and never raise it
    handler, _ = _handler(tmp_path / "b", transport_for(), max_reads_per_pass=1)
    assert handler.fill(ModelJobLogFillRequest(max_reads=5)).deferred == 1


def test_a_pass_that_cannot_start_answers_failed_naming_why(tmp_path: Path) -> None:
    def broken() -> dict[str, Any]:
        raise ValueError("watcher state unreadable")

    handler = HandlerJobLogFiller(
        overlay=overlay(),
        reader_factory=lambda _p: None,
        state_reader=broken,
    )
    result = handler.fill(ModelJobLogFillRequest())
    assert result.status is EnumJobLogFillStatus.FAILED
    assert "watcher state unreadable" in (result.error or "")


# ---------------------------------------------------------- the failing control


def _lane_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: int):
    root = tmp_path / "job-logs"
    overlay_dir = tmp_path / "github_job_logs"
    overlay_dir.mkdir(exist_ok=True)
    (overlay_dir / "overlay.yaml").write_text(f"root: {root}\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    async def forbidden():
        pytest.fail("a lane job-log read resolved a GitHub credential")

    monkeypatch.setattr(gateway, "resolve_github_token_async", forbidden)
    return CliRunner().invoke(
        scm_group,
        [
            "job-log",
            REPO,
            "--job-id",
            str(job),
            "--bus",
            "inmemory",
            "--timeout-s",
            "3",
        ],
    )


def test_control_lane_read_refuses_naming_the_cache_path_then_answers_after_the_filler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = _lane_read(tmp_path, monkeypatch, RED_JOB)
    assert refused.exit_code == 1, refused.output
    assert "JOB-LOG-CACHE-MISS" in refused.output
    assert (
        str(tmp_path / "job-logs") in refused.output
    )  # the refusal names the cache path

    transport = transport_for(RED_JOB, SUMMARY_JOB)
    handler, _ = _handler(tmp_path, transport)
    assert handler.fill(ModelJobLogFillRequest()).filled == 2

    answered = _lane_read(tmp_path, monkeypatch, RED_JOB)
    assert answered.exit_code == 0, answered.output
    assert json.loads(answered.output)["result"]["text"] == RED_LOG.decode()
    assert len(transport.sent) == 2  # the lane read made no GitHub call


class _NothingReader:
    """The injected break: a reader that downloads nothing and writes no cache."""

    cache_path = None

    def read(
        self,
        repo: str,
        job_id: int,
        *,
        cache_only: bool,
        tail_bytes: int | None = None,
    ) -> ModelJobLogTailResult:
        if cache_only:
            raise JobLogCacheMissError("miss")
        return ModelJobLogTailResult(
            repo=repo, job_id=job_id, total_bytes=0, truncated=False, text=""
        )


def test_injected_break_a_filler_that_fetches_nothing_leaves_the_lane_read_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = HandlerJobLogFiller(
        overlay=overlay(),
        reader_factory=lambda _p: _NothingReader(),
        state_reader=watcher_state,
    )
    result = handler.fill(ModelJobLogFillRequest())
    assert result.filled == 2  # the pass believes it filled them
    still = _lane_read(tmp_path, monkeypatch, RED_JOB)
    assert still.exit_code == 1
    assert "JOB-LOG-CACHE-MISS" in still.output
    assert str(tmp_path / "job-logs") in still.output


def test_the_overlay_can_turn_the_lane_default_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        lane_reads_cache_only,
    )

    assert lane_reads_cache_only() is True  # the contract declares it
    overlay_dir = tmp_path / "github_job_logs"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text("lane_reads_cache_only: false\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert lane_reads_cache_only() is False


# ------------------------------------------------------- the Redpanda profile's reader


def test_the_bus_reader_names_the_profile_lane_and_maps_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from omnimarket.handlers.scm.handler_gateway_read_client import GATEWAY_BUS_LANE_ENV
    from omnimarket.models.model_gateway_node_skew import GatewayReadError
    from omnimarket.nodes.node_job_log_filler_effect.handlers import (
        adapter_job_log_readers as adapters,
    )
    from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
        build_reader,
    )

    seen: list[tuple[bool, str | None]] = []

    def fake_read_over_bus(request, *, timeout_s):
        seen.append((request.cache_only, os.environ.get(GATEWAY_BUS_LANE_ENV)))
        if request.cache_only:
            raise GatewayReadError(
                "gateway job_log_tail: JOB-LOG-CACHE-MISS: no GitHub call made"
            )
        return ModelJobLogTailResult(
            repo=request.repo,
            job_id=request.job_id,
            total_bytes=3,
            truncated=False,
            text="abc",
        )

    monkeypatch.setattr(adapters, "read_over_bus", fake_read_over_bus)
    reader = build_reader(overlay(), "redpanda")
    assert reader.cache_path is None  # the gateway's cache is its own
    with pytest.raises(JobLogCacheMissError):
        reader.read(REPO, RED_JOB, cache_only=True)
    assert reader.read(REPO, RED_JOB, cache_only=False).total_bytes == 3
    assert seen == [(True, "example-lane"), (False, "example-lane")]


def test_a_runtime_failure_terminal_becomes_a_failed_result() -> None:
    from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_filler_cli import (
        result_from_terminal,
    )

    request = ModelJobLogFillRequest()
    result = result_from_terminal(
        {"failure_reason": "overlay not configured", "failure_class": "X"},
        request,
        "inmemory",
    )
    assert result.status is EnumJobLogFillStatus.FAILED
    assert result.error == "overlay not configured"
