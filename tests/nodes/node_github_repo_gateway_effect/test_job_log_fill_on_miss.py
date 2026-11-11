# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23308: a lane's job-log read that misses the cache is filled for that one job.

The gateway's ``job_log_fill`` operation serves a cached log, or on a miss
downloads that one job's log into the host-shared cache and returns it. The
cache-only ``job-log`` read stays a refusal; the fill never widens it.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.handlers.scm.handler_scm_dispatch import SCM_VERBS
from omnimarket.handlers.scm.scm_cli import scm_group
from omnimarket.models.github_landing.model_github_transport_error import (
    GitHubTransportError,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
    ModelJobLogFillResult,
    ModelJobLogTailResult,
)
from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
    handler_github_repo_gateway as gateway,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    BytesExchange,
    RecordedGatewayTransport,
    get,
)

pytestmark = pytest.mark.unit
REPO = "OmniNode-ai/omnimarket"
JOB = 129091071572
PATH = f"/repos/{REPO}/actions/jobs/{JOB}/logs"
LOG = b"setup\n##[error]Process completed with exit code 1.\n"


def _fill_request(**fields: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.JOB_LOG_FILL,
        repo=REPO,
        job_id=JOB,
        **fields,
    )


def _recorded(count: int = 1) -> RecordedGatewayTransport:
    return RecordedGatewayTransport(
        [BytesExchange(get(PATH), 200, LOG, 64 * 1024 * 1024, "window")] * count
    )


def _cache_at(tmp_path, monkeypatch) -> None:
    overlay = tmp_path / "github_job_logs"
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(f"root: {tmp_path / 'bytes'}\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _wire_gateway(monkeypatch, transport: RecordedGatewayTransport) -> list[str]:
    credentials: list[str] = []

    async def token() -> str:
        credentials.append("resolved")
        return "token-not-printed"

    monkeypatch.setattr(gateway, "resolve_github_token_async", token)
    monkeypatch.setattr(gateway, "RealGitHubReadTransport", lambda _token: transport)
    return credentials


def _handle(
    request: ModelGithubGatewayRequest,
) -> ModelJobLogTailResult | ModelJobLogFillResult:
    output = asyncio.run(gateway.HandlerGithubRepoGatewayEffect().handle(request))
    result = cast(ModelGithubGatewayResponse, output.events[0]).result
    assert isinstance(result, ModelJobLogTailResult | ModelJobLogFillResult)
    assert result.operation == request.operation.value
    return result


def test_job_log_fill_on_miss_downloads_the_one_job_and_returns_it(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch)
    transport = _recorded()
    credentials = _wire_gateway(monkeypatch, transport)

    first = _handle(_fill_request())
    assert isinstance(first, ModelJobLogFillResult)
    assert first.text == LOG.decode()
    assert first.job_id == JOB
    assert first.total_bytes == len(LOG)
    assert len(transport.sent) == 1
    assert credentials == ["resolved"]
    assert list((tmp_path / "bytes").glob("*.log"))

    # The second read is a hit: no further GitHub request.
    second = _handle(_fill_request())
    assert second == first
    assert len(transport.sent) == 1


def test_job_log_fill_on_miss_serves_the_filled_log_to_the_cache_only_read(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch)
    transport = _recorded()
    _wire_gateway(monkeypatch, transport)

    # Control: before the fill, the cache-only read refuses the same job.
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

    _handle(_fill_request())
    after = _handle(
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
            repo=REPO,
            job_id=JOB,
            cache_only=True,
        )
    )
    assert after.text == LOG.decode()
    assert len(transport.sent) == 1


def test_job_log_fill_on_miss_through_the_scm_verb(tmp_path, monkeypatch):
    _cache_at(tmp_path, monkeypatch)
    transport = _recorded()
    _wire_gateway(monkeypatch, transport)

    assert "job-log-fill" in {spec.verb for spec in SCM_VERBS}
    result = CliRunner().invoke(
        scm_group,
        [
            "job-log-fill",
            REPO,
            "--job-id",
            str(JOB),
            "--bus",
            "inmemory",
            "--timeout-s",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["verb"] == "job-log-fill"
    assert answer["operation"] == "job_log_fill"
    assert answer["outcome"] == "completed"
    assert answer["result"]["text"] == LOG.decode()
    assert len(transport.sent) == 1


def test_job_log_fill_names_a_failed_download_instead_of_a_partial_log(
    tmp_path, monkeypatch
):
    _cache_at(tmp_path, monkeypatch)
    transport = RecordedGatewayTransport(
        [BytesExchange(get(PATH), 404, b"Not Found", 64 * 1024 * 1024, "window")]
    )
    _wire_gateway(monkeypatch, transport)
    with pytest.raises(GitHubTransportError):
        _handle(_fill_request())
    assert not list((tmp_path / "bytes").glob("*.log"))


def test_job_log_fill_requires_a_job_and_never_cache_only():
    with pytest.raises(ValidationError, match="requires job_id"):
        ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.JOB_LOG_FILL, repo=REPO
        )
    with pytest.raises(ValidationError, match="cannot be cache_only"):
        _fill_request(cache_only=True)
