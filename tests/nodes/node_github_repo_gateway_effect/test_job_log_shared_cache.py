# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the eight-read barrier: later windows of a downloaded job are local."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from omnimarket.models.model_github_gateway_io import ModelGithubGatewayRequest
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    BytesExchange,
    RecordedGatewayTransport,
    get,
)

pytestmark = pytest.mark.unit
REPO = "OmniNode-ai/omnimarket"
JOB = 124958464244
PATH = f"/repos/{REPO}/actions/jobs/{JOB}/logs"


def request(**fields):
    return ModelGithubGatewayRequest(
        operation="job_log_tail", repo=REPO, job_id=JOB, **fields
    )


def test_recorded_long_job_reaches_failure_after_eight_windows(tmp_path):
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )

    # Recorded cache: this job is 601957 bytes, while a reply keeps <=65536.
    # Place a synthetic failure in window nine to expose the recorded size barrier.
    marker = b"##[error]Process completed with exit code 1.\n"
    body = b"x" * (8 * 65536) + marker
    body += b"z" * (601957 - len(body))
    transport = RecordedGatewayTransport(
        [BytesExchange(get(PATH), 200, body, 64 * 1024 * 1024, "window")]
    )
    cache = HandlerJobLogCache(tmp_path, transport)
    first = cache.handle(request(offset_bytes=0, length_bytes=65536))
    assert first.next_offset == 65536
    for offset in range(65536, 8 * 65536, 65536):
        assert (
            cache.handle(
                request(offset_bytes=offset, length_bytes=65536, cache_only=True)
            ).text
            == "x" * 65536
        )
    ninth = HandlerJobLogCache(tmp_path, None).handle(
        request(offset_bytes=8 * 65536, length_bytes=65536, cache_only=True)
    )
    assert marker.decode() in ninth.text
    assert ninth.start_offset == 8 * 65536
    assert ninth.total_bytes == 601957
    assert len(transport.sent) == 1
    transport.assert_drained()


def test_concurrent_lanes_download_one_job_once(tmp_path):
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )

    body = b"failure\n" * 10000
    transport = RecordedGatewayTransport(
        [BytesExchange(get(PATH), 200, body, 64 * 1024 * 1024, "window")]
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: HandlerJobLogCache(tmp_path, transport).handle(request()),
                range(4),
            )
        )
    assert all(result.text.endswith("failure\n") for result in results)
    assert len(transport.sent) == 1


def test_cache_only_miss_never_resolves_a_credential_or_opens_http(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from omnimarket.handlers.scm.scm_cli import scm_group
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
        handler_github_repo_gateway as gateway,
    )

    overlay = tmp_path / "github_job_logs"
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(f"root: {tmp_path / 'bytes'}\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    async def forbidden():
        pytest.fail("cache-only request resolved a GitHub credential")

    monkeypatch.setattr(gateway, "resolve_github_token_async", forbidden)
    result = CliRunner().invoke(
        scm_group,
        [
            "job-log-cached",
            REPO,
            "--job-id",
            str(JOB),
            "--bus",
            "inmemory",
            "--timeout-s",
            "3",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "JOB-LOG-CACHE-MISS" in result.output


def test_cache_only_bus_command_reaches_the_ninth_window(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from omnimarket.handlers.scm.scm_cli import scm_group
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
        handler_github_repo_gateway as gateway,
    )
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )

    root = tmp_path / "bytes"
    overlay = tmp_path / "github_job_logs"
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(f"root: {root}\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    body = b"x" * (8 * 65536) + b"failure traceback\n"
    transport = RecordedGatewayTransport(
        [BytesExchange(get(PATH), 200, body, 64 * 1024 * 1024, "window")]
    )
    HandlerJobLogCache(root, transport).handle(request())

    async def forbidden():
        pytest.fail("cache-only request resolved a GitHub credential")

    monkeypatch.setattr(gateway, "resolve_github_token_async", forbidden)
    result = CliRunner().invoke(
        scm_group,
        [
            "job-log-cached",
            REPO,
            "--job-id",
            str(JOB),
            "--offset-bytes",
            str(8 * 65536),
            "--length-bytes",
            "65536",
            "--bus",
            "inmemory",
            "--timeout-s",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer["result"]["text"] == "failure traceback\n"
    assert answer["result"]["start_offset"] == 8 * 65536
    assert answer["source"] == "bus"
    assert answer["bus"]["terminal_envelope_id"]
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    "damage", ["expired", "same_size_corruption", "truncated", "metadata"]
)
def test_unreadable_cache_fails_closed_without_network(tmp_path, damage):
    import os
    import time

    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )
    from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

    transport = RecordedGatewayTransport(
        [BytesExchange(get(PATH), 200, b"failure\n", 64 * 1024 * 1024, "window")]
    )
    cache = HandlerJobLogCache(tmp_path, transport)
    cache.handle(request())
    if damage == "expired":
        os.utime(cache.meta, (time.time() - 90000,) * 2)
    elif damage == "same_size_corruption":
        cache.raw.write_bytes(b"garbage\n")
    elif damage == "truncated":
        cache.raw.write_bytes(b"x")
    else:
        cache.meta.write_text("broken")
    with pytest.raises(GitHubTransportError, match="CACHE-MISS"):
        HandlerJobLogCache(tmp_path, None).handle(request(cache_only=True))
    assert len(transport.sent) == 1


def test_completed_step_metadata_is_reused_with_cached_bytes(tmp_path):
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )
    from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
        JsonExchange,
        json_response,
    )

    body = (
        b"2026-11-05T09:00:00Z setup\n"
        b"2026-11-05T09:00:01Z failure traceback\n"
        b"2026-11-05T09:00:02Z cleanup\n"
    )
    transport = RecordedGatewayTransport(
        [
            JsonExchange(
                get(PATH.removesuffix("/logs")),
                json_response(
                    {
                        "status": "completed",
                        "steps": [
                            {
                                "number": 2,
                                "name": "Tests",
                                "status": "completed",
                                "conclusion": "failure",
                                "started_at": "2026-11-05T09:00:01Z",
                                "completed_at": "2026-11-05T09:00:01Z",
                            }
                        ],
                    }
                ),
            ),
            BytesExchange(get(PATH), 200, body, 64 * 1024 * 1024, "window"),
        ]
    )
    first = HandlerJobLogCache(tmp_path, transport).handle(request(step="Tests"))
    second = HandlerJobLogCache(tmp_path, None).handle(
        request(step="Tests", cache_only=True)
    )
    assert second == first
    assert second.text == "2026-11-05T09:00:01Z failure traceback\n"
    assert len(transport.sent) == 2


def test_legacy_partial_log_supplies_only_covered_bytes(tmp_path):
    import json

    from omnimarket.models.model_github_gateway_io import ModelJobLogTailResult
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )
    from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

    # Recorded real red job's cached geometry: the last 16384 of 117426 bytes.
    root = tmp_path / "job-logs"
    marker = "##[error]Process completed with exit code 1.\n"
    text = "x" * (16384 - len(marker)) + marker
    saved = ModelJobLogTailResult(
        repo=REPO,
        job_id=JOB,
        total_bytes=117426,
        truncated=True,
        start_offset=101042,
        text=text,
    )
    (tmp_path / "old-command-key.json").write_text(
        json.dumps(
            {
                "stdout": json.dumps(
                    {
                        "verb": "job-log",
                        "outcome": "completed",
                        "result": saved.model_dump(mode="json"),
                    }
                ),
                "stderr": "",
            }
        )
    )
    cache = HandlerJobLogCache(root, None)
    result = cache.handle(
        request(cache_only=True, offset_bytes=101100, length_bytes=200)
    )
    assert result.text == "x" * 200
    assert result.total_bytes == 117426
    assert result.start_offset == 101100
    assert result.next_offset == 101300
    assert result.truncated
    with pytest.raises(GitHubTransportError, match="CACHE-MISS"):
        cache.handle(request(cache_only=True, offset_bytes=0, length_bytes=200))
    other = request(cache_only=True).model_copy(update={"job_id": JOB + 1})
    with pytest.raises(GitHubTransportError, match="CACHE-MISS"):
        cache.handle(other)


def test_failed_download_is_not_cached_and_can_be_retried(tmp_path):
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        HandlerJobLogCache,
    )
    from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

    transport = RecordedGatewayTransport(
        [
            BytesExchange(get(PATH), 503, b"unavailable", 64 * 1024 * 1024, "window"),
            BytesExchange(get(PATH), 200, b"failure\n", 64 * 1024 * 1024, "window"),
        ]
    )
    with pytest.raises(GitHubTransportError, match="HTTP 503"):
        HandlerJobLogCache(tmp_path, transport).handle(request())
    assert not list(tmp_path.glob("*.log"))
    assert not list(tmp_path.glob("*.tmp"))
    assert HandlerJobLogCache(tmp_path, transport).handle(request()).text == "failure\n"
    assert len(transport.sent) == 2


def test_stream_ceiling_never_publishes_an_incomplete_cache(tmp_path, monkeypatch):
    from omnimarket.github_landing import github_landing_transport
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers import (
        handler_github_repo_gateway as handler_job_log_cache,
    )
    from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

    monkeypatch.setattr(github_landing_transport, "MAX_LOG_READ_BYTES", 16)
    monkeypatch.setattr(handler_job_log_cache, "MAX_LOG_READ_BYTES", 16)
    transport = RecordedGatewayTransport(
        [
            BytesExchange(get(PATH), 200, b"x" * 17, 16, "window"),
        ]
    )
    with pytest.raises(GitHubTransportError, match="read_ceiling_hit=True"):
        handler_job_log_cache.HandlerJobLogCache(tmp_path, transport).handle(request())
    assert not list(tmp_path.glob("*.log"))
    assert not list(tmp_path.glob("*.meta.json"))
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(GitHubTransportError, match="CACHE-MISS"):
        handler_job_log_cache.HandlerJobLogCache(tmp_path, None).handle(
            request(cache_only=True)
        )
    assert len(transport.sent) == 1


def test_cache_policy_uses_declared_state_root(tmp_path, monkeypatch):
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        cache_policy,
    )

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    assert cache_policy().root == tmp_path / "github-read-cache" / "job-logs"


def test_cache_policy_refuses_an_unconfigured_state_root(tmp_path, monkeypatch):
    from omnimarket.config.state_root import OnexStateRootUnconfiguredError
    from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
        cache_policy,
    )

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)
    with pytest.raises(OnexStateRootUnconfiguredError, match="GitHub job-log cache"):
        cache_policy()
