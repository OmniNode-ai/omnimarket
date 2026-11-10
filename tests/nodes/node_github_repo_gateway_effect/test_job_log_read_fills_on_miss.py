# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23378: a lane's ``job-log`` read fills a cache miss inside a declared budget.

The gateway's declared policy names how many on-demand fills a lane read may
spend per window (``lane_miss_fill_max``). With a budget, a ``job_log_tail``
request with ``fill_on_miss`` downloads that one job's log into the shared
cache and answers it; with no budget the miss is refused exactly like
``cache_only``. The filler node's own reads share the same cache entries, and
the ``job-log-cached`` verb never turns into a live read.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.handlers.scm.scm_cli import scm_group
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelJobLogTailResult,
)
from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
    handler_github_repo_gateway as gateway,
)
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.miss_fill_budget import (
    MissFillBudget,
    MissFillBudgetSpentError,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_job_log_cache import (
    ModelJobLogCachePolicy,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers import adapter_job_log_readers
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    BytesExchange,
    RecordedGatewayTransport,
    get,
)

pytestmark = pytest.mark.unit
REPO = "OmniNode-ai/omnimarket"
JOB = 129091071572
OTHER = 129091071573
LOG = b"setup\n##[error]Process completed with exit code 1.\n"


def _path(job: int) -> str:
    return f"/repos/{REPO}/actions/jobs/{job}/logs"


def _lane_request(job_id: int = JOB, **fields: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
        repo=REPO,
        job_id=job_id,
        fill_on_miss=True,
        **fields,
    )


def _recorded(*jobs: int) -> RecordedGatewayTransport:
    return RecordedGatewayTransport(
        [
            BytesExchange(get(_path(job)), 200, LOG, 64 * 1024 * 1024, "window")
            for job in jobs
        ]
    )


def _cache_at(tmp_path, monkeypatch, *overlay_lines: str) -> None:
    overlay = tmp_path / "github_job_logs"
    overlay.mkdir()
    lines = [f"root: {tmp_path / 'bytes'}", *overlay_lines]
    (overlay / "overlay.yaml").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _stamps(cache_root) -> list[str]:
    log = cache_root / "miss-fills.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.strip()]


def _wire_gateway(monkeypatch, transport: RecordedGatewayTransport) -> None:
    async def token() -> str:
        return "token-not-printed"

    monkeypatch.setattr(gateway, "resolve_github_token_async", token)
    monkeypatch.setattr(gateway, "RealGitHubReadTransport", lambda _token: transport)


def _capturing(monkeypatch) -> list[ModelGithubGatewayRequest]:
    captured: list[ModelGithubGatewayRequest] = []
    original = gateway.HandlerGithubRepoGatewayEffect.handle

    async def spy(self, request: ModelGithubGatewayRequest):
        captured.append(request)
        return await original(self, request)

    monkeypatch.setattr(gateway.HandlerGithubRepoGatewayEffect, "handle", spy)
    return captured


def _invoke(job_id: int, verb: str = "job-log", *options: str):
    return CliRunner().invoke(
        scm_group,
        [
            verb,
            REPO,
            "--job-id",
            str(job_id),
            "--bus",
            "inmemory",
            "--timeout-s",
            "5",
            *options,
        ],
    )


def _handle(request: ModelGithubGatewayRequest) -> ModelJobLogTailResult:
    output = asyncio.run(gateway.HandlerGithubRepoGatewayEffect().handle(request))
    result = cast(ModelGithubGatewayResponse, output.events[0]).result
    assert isinstance(result, ModelJobLogTailResult)
    assert result.operation == request.operation.value
    return result


def test_fill_on_miss_is_job_log_tail_only_and_exclusive_with_cache_only():
    with pytest.raises(
        ValidationError, match="fill_on_miss is supported only for job_log_tail"
    ):
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.PR_STATUS,
            repo=REPO,
            pr_number=7,
            fill_on_miss=True,
        )
    with pytest.raises(
        ValidationError, match="fill_on_miss and cache_only are exclusive"
    ):
        _lane_request(cache_only=True)


def test_the_cache_policy_declares_the_miss_fill_budget_fields():
    default = ModelJobLogCachePolicy()
    assert default.lane_miss_fill_max == 0
    assert default.lane_miss_fill_window_seconds == 3600
    declared = ModelJobLogCachePolicy.model_validate(
        {"lane_miss_fill_max": 2, "lane_miss_fill_window_seconds": 60}
    )
    assert (declared.lane_miss_fill_max, declared.lane_miss_fill_window_seconds) == (
        2,
        60,
    )


def test_lane_read_fills_the_miss_once_and_the_second_read_is_a_hit(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 1\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)

    first = _handle(_lane_request())
    assert first.text == LOG.decode()
    assert first.job_id == JOB
    assert len(transport.sent) == 1
    assert len(_stamps(tmp_path / "bytes")) == 1

    # The second read is a hit: no further GitHub request, no further fill spent.
    second = _handle(_lane_request())
    assert second == first
    assert len(transport.sent) == 1
    assert len(_stamps(tmp_path / "bytes")) == 1


def test_a_spent_miss_fill_budget_refuses_the_miss_without_a_request(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 1\n")
    transport = _recorded(JOB, OTHER)
    _wire_gateway(monkeypatch, transport)
    _handle(_lane_request())  # spends the window's one fill

    with pytest.raises(GitHubTransportError, match="JOB-LOG-CACHE-MISS") as spent:
        _handle(_lane_request(OTHER))
    assert "miss-fill budget" in str(spent.value)
    assert len(transport.sent) == 1


def test_without_a_declared_fill_budget_a_fill_on_miss_read_refuses_the_miss(
    tmp_path, monkeypatch
):
    # No lane_miss_fill_max line: the overlay's default budget is none.
    _cache_at(tmp_path, monkeypatch)
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)

    with pytest.raises(GitHubTransportError, match="JOB-LOG-CACHE-MISS"):
        _handle(_lane_request())
    assert transport.sent == []


def test_control_without_fill_on_miss_the_lane_read_still_refuses(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)

    with pytest.raises(GitHubTransportError, match="JOB-LOG-CACHE-MISS"):
        _handle(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
                repo=REPO,
                job_id=JOB,
                cache_only=True,
            )
        )
    assert transport.sent == []


def test_miss_fill_budget_ages_entries_out_of_the_window(tmp_path):
    now = 1_000.0
    budget = MissFillBudget(
        tmp_path / "budget", max_fills=1, window_seconds=100, clock=lambda: now
    )
    budget.spend()
    with pytest.raises(MissFillBudgetSpentError):
        budget.spend()

    # The first entry ages out of the window, so one more fill is allowed.
    now += 101
    budget.spend()
    assert len(_stamps(tmp_path / "budget")) == 1


def test_the_scm_verb_fills_the_miss_under_the_declared_budget(tmp_path, monkeypatch):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)

    result = _invoke(JOB)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["verb"] == "job-log"
    assert answer["outcome"] == "completed"
    assert answer["result"]["text"] == LOG.decode()
    assert len(transport.sent) == 1


def test_the_scm_verb_sets_cache_only_when_the_overlay_declares_no_budget(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 0\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)

    result = _invoke(JOB)
    assert result.exit_code != 0
    assert "JOB-LOG-CACHE-MISS" in result.output
    assert transport.sent == []


def test_the_scm_verb_sets_no_flag_when_lane_reads_are_not_cache_only(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_reads_cache_only: false\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)
    captured = _capturing(monkeypatch)

    result = _invoke(JOB)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["text"] == LOG.decode()
    assert captured[0].fill_on_miss is False
    assert captured[0].cache_only is False
    assert len(transport.sent) == 1


def test_job_log_cached_stays_cache_only_whatever_the_policy(tmp_path, monkeypatch):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)
    captured = _capturing(monkeypatch)

    result = _invoke(JOB, "job-log-cached")
    assert result.exit_code != 0
    assert "JOB-LOG-CACHE-MISS" in result.output
    assert captured[0].cache_only is True
    assert captured[0].fill_on_miss is False
    assert transport.sent == []


def _filler_reader(tmp_path, transport) -> adapter_job_log_readers.InMemoryJobLogReader:
    return adapter_job_log_readers.InMemoryJobLogReader(
        tmp_path / "bytes",
        ttl_seconds=86400,
        tail_bytes=4096,
        transport_factory=lambda: transport,
    )


def test_seam_the_filler_and_the_lane_share_one_cache_entry(tmp_path, monkeypatch):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)
    _filler_reader(tmp_path, transport).read(REPO, JOB, cache_only=False)
    assert len(transport.sent) == 1

    # The lane's fill_on_miss read of the filled job is a hit: no extra request,
    # and nothing was spent from the miss-fill budget.
    filled = _handle(_lane_request())
    assert filled.text == LOG.decode()
    assert len(transport.sent) == 1
    assert _stamps(tmp_path / "bytes") == []


def test_seam_a_job_the_filler_has_not_filled_spends_exactly_one_fill(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB, OTHER)
    _wire_gateway(monkeypatch, transport)
    _filler_reader(tmp_path, transport).read(REPO, JOB, cache_only=False)

    lane = _handle(_lane_request(OTHER))
    assert lane.text == LOG.decode()
    assert len(transport.sent) == 2
    assert len(_stamps(tmp_path / "bytes")) == 1


def test_seam_the_verb_request_round_trips_and_matches_the_fillers_shape(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)
    captured = _capturing(monkeypatch)

    result = _invoke(JOB, "job-log", "--tail-bytes", "4096")
    assert result.exit_code == 0, result.output
    lane = captured[0]
    assert lane.fill_on_miss is True

    round_tripped = ModelGithubGatewayRequest.model_validate_json(
        lane.model_dump_json()
    )
    assert round_tripped == lane
    assert round_tripped.fill_on_miss is True

    # The filler's own request builder differs only in fill_on_miss.
    fillers = adapter_job_log_readers._request(
        REPO, JOB, cache_only=False, tail_bytes=4096
    )
    lane_fields = lane.model_dump(exclude={"correlation_id"})
    filler_fields = fillers.model_dump(exclude={"correlation_id"})
    lane_fill = lane_fields.pop("fill_on_miss")
    filler_fill = filler_fields.pop("fill_on_miss")
    assert lane_fill is True
    assert filler_fill is False
    assert lane_fields == filler_fields


def test_seam_a_gateway_older_than_the_client_is_asked_cache_only(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch, "lane_miss_fill_max: 30\n")
    transport = _recorded(JOB)
    _wire_gateway(monkeypatch, transport)
    seen: list[ModelGithubGatewayRequest] = []
    original = gateway.HandlerGithubRepoGatewayEffect.handle

    async def spy(self, request: ModelGithubGatewayRequest):
        seen.append(request)
        if request.fill_on_miss:
            # The older node's own validation: the request field it does not know.
            raise ValidationError.from_exception_data(
                "ModelGithubGatewayRequest",
                [
                    {
                        "type": "extra_forbidden",
                        "loc": ("fill_on_miss",),
                        "input": request.fill_on_miss,
                    }
                ],
            )
        return await original(self, request)

    monkeypatch.setattr(gateway.HandlerGithubRepoGatewayEffect, "handle", spy)

    result = _invoke(JOB)
    assert len(seen) == 2, result.output
    assert seen[0].fill_on_miss is True
    assert seen[1].fill_on_miss is False
    assert seen[1].cache_only is True
    assert result.exit_code != 0
    assert "JOB-LOG-CACHE-MISS" in result.output
