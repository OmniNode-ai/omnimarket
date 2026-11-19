# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25857: a job-log request the gateway cannot serve answers a typed refusal in bounded time.

The fixtures under ``fixtures/`` are the dev runtime container's own log lines and its
miss-fill ledger, recorded on 2026-11-19: the container started 03:30:48Z (kernel up
03:30:54Z), attached the gateway consumer at 03:34:55Z, and answered a cache miss with a
typed failure naming the spent budget. A request published before the attach was never
answered. The tests below hold the node itself to the same promise: a request whose job
lock is held, or whose download does not end, ends in a typed refusal inside the declared
bounds, never in silence.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
)
from omnimarket.models.github_landing.model_transport_error import GitHubTransportError
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
    handler_github_repo_gateway as gateway,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_job_log_cache import (
    ModelJobLogCachePolicy,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
CONSUMER_LOG = FIXTURES / "dev_runtime_gateway_consumer_2026-11-19.txt"
MISS_FILLS = FIXTURES / "miss_fills_2026-11-19.txt"
REPO = "OmniNode-ai/omnimarket"
RECORDED_MISS_JOB = 135275292608
LOG = b"setup\n##[error]Process completed with exit code 1.\n"


def _recorded(pattern: str) -> str:
    matches = [
        line
        for line in CONSUMER_LOG.read_text().splitlines()
        if re.search(pattern, line)
    ]
    assert len(matches) == 1, pattern
    return matches[0]


def _policy(root: Path, **declared: float) -> ModelJobLogCachePolicy:
    # model_copy does not validate: a field the policy does not know yet is ignored, so the
    # same call runs against the unfixed handler and shows what it does under the lock.
    base = ModelJobLogCachePolicy(
        root=root, lane_miss_fill_max=30, lane_miss_fill_window_seconds=3600
    )
    return base.model_copy(update=declared)


def _request(job: int, operation=EnumGithubGatewayOperation.JOB_LOG_FILL):
    return ModelGithubGatewayRequest(operation=operation, repo=REPO, job_id=job)


def _handle(request: ModelGithubGatewayRequest) -> None:
    asyncio.run(gateway.HandlerGithubRepoGatewayEffect().handle(request))


def _wire(monkeypatch, root: Path, transport, **declared: float) -> None:
    async def token() -> str:
        return "token-not-printed"

    monkeypatch.setattr(gateway, "resolve_github_token_async", token)
    monkeypatch.setattr(gateway, "RealGitHubReadTransport", lambda _token: transport)
    monkeypatch.setattr(gateway, "cache_policy", lambda: _policy(root, **declared))


class _OneShotTransport:
    """Serves the log once, in one chunk."""

    def send_bytes_window_sync(
        self, request: ModelGithubHttpRequest, *, window: gateway.JobLogWindow
    ) -> ModelGithubBytesResponse:
        window.feed(LOG)
        return ModelGithubBytesResponse(
            status=200,
            headers={},
            content=LOG,
            total_bytes=len(LOG),
            over_limit=False,
            keep="window",
        )

    def send_sync(self, request):
        raise AssertionError("not used")


class _TrickleTransport(_OneShotTransport):
    """Feeds one small chunk every 50 ms for as long as the node keeps reading."""

    def send_bytes_window_sync(
        self, request: ModelGithubHttpRequest, *, window: gateway.JobLogWindow
    ) -> ModelGithubBytesResponse:
        for _ in range(100):
            window.feed(b"x")
            time.sleep(0.05)
        return super().send_bytes_window_sync(request, window=window)


def _lock_path(root: Path, job: int) -> Path:
    key = hashlib.sha256(f"{REPO.lower()}:{job}".encode()).hexdigest()
    return root / f"{key}.lock"


def test_recorded_miss_refusal_names_the_spent_budget_and_the_cache_path(
    tmp_path, monkeypatch
):
    """Control: the settled runtime's miss refusal, replayed from the recorded ledger."""
    recorded = _recorded(
        r"service_kernel: Dispatcher .* failed: GitHubTransportError: JOB-LOG-CACHE-MISS"
    )
    assert (
        f"job {RECORDED_MISS_JOB} of {REPO} is not under the job-log cache path"
        in recorded
    )
    assert "lane miss-fill budget (30 fills per 3600 s) is spent" in recorded

    stamps = [
        datetime.strptime(f"2026-11-19 {line}", "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=UTC)
        .timestamp()
        for line in MISS_FILLS.read_text().split()
    ]
    assert len(stamps) == 30
    # Shift the recorded ledger so its last fill is one second old, as at 03:21:11Z for a
    # request at 03:21:12Z, then replay the same request.
    shift = time.time() - 1 - stamps[-1]
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "miss-fills.log").write_text(
        "\n".join(repr(s + shift) for s in stamps) + "\n"
    )
    _wire(monkeypatch, tmp_path, _OneShotTransport())

    with pytest.raises(GitHubTransportError) as refusal:
        _handle(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
                repo=REPO,
                job_id=RECORDED_MISS_JOB,
                fill_on_miss=True,
            )
        )
    assert str(refusal.value).startswith(
        recorded.split("GitHubTransportError: ")[1][:80]
    )


def test_request_whose_job_lock_is_held_answers_job_log_busy_inside_the_bound(
    tmp_path, monkeypatch
):
    _wire(monkeypatch, tmp_path, _OneShotTransport(), lock_wait_seconds=0.3)
    holder = _lock_path(tmp_path, RECORDED_MISS_JOB).open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            _handle(_request(RECORDED_MISS_JOB))
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=5)
    answered_in = time.monotonic() - started
    still_silent = worker.is_alive()
    fcntl.flock(holder, fcntl.LOCK_UN)  # let an unfixed handler finish before asserting
    worker.join(timeout=5)
    holder.close()

    assert not still_silent, "the request is still waiting on the held job lock"
    assert answered_in < 3
    assert isinstance(outcome[0], GitHubTransportError)
    assert str(outcome[0]).startswith("JOB-LOG-BUSY:")


def test_a_free_job_lock_is_served_as_before(tmp_path, monkeypatch):
    """Control: with no holder the same request fills and returns the log."""
    _wire(monkeypatch, tmp_path, _OneShotTransport(), lock_wait_seconds=0.3)
    _handle(_request(RECORDED_MISS_JOB))
    assert list(tmp_path.glob("*.log"))


def test_download_that_outlasts_its_deadline_answers_fill_timeout(
    tmp_path, monkeypatch
):
    _wire(
        monkeypatch,
        tmp_path,
        _TrickleTransport(),
        download_deadline_seconds=0.3,
    )
    started = time.monotonic()
    with pytest.raises(GitHubTransportError) as refusal:
        _handle(_request(RECORDED_MISS_JOB))
    assert str(refusal.value).startswith("JOB-LOG-FILL-TIMEOUT:")
    assert time.monotonic() - started < 3
    assert not list(tmp_path.glob("*.meta.json")), (
        "a timed-out download is never cached"
    )
