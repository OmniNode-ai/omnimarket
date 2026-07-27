# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for AdapterInferenceBridge.

Covers all branches without live infrastructure:
- __init__ stores config
- infer(): unknown key → ValueError; cli transport → _call_cli_model;
  http/default transport → _call_http_model
- _call_http_model(): missing base_url / base_url_env → ValueError;
  missing model_id → ValueError; api_key → Authorization header;
  reserved extra_headers → warning + skip; temperature arg vs cfg;
  successful POST → parses choices[0].message.content
- _call_cli_model(): missing cli_command → ValueError;
  asyncio.create_subprocess_exec + communicate returns decoded stdout.strip();
  does not block the event loop; timeout maps to subprocess.TimeoutExpired
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HTTP_MODULE = "httpx.AsyncClient"
_CREATE_SUBPROCESS = "asyncio.create_subprocess_exec"
_BRIDGE_LOGGER = "omnimarket.inference.adapter_inference_bridge"


def _make_mock_httpx_cm(content: str) -> tuple[MagicMock, AsyncMock]:
    """Return (context-manager mock, inner client mock) yielding a response with *content*."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {"content": content}}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def http_config() -> ModelInferenceBridgeConfig:
    return ModelInferenceBridgeConfig(
        model_configs={
            "test-http": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
            }
        }
    )


@pytest.fixture
def cli_config() -> ModelInferenceBridgeConfig:
    return ModelInferenceBridgeConfig(
        model_configs={
            "test-cli": {
                "transport": "cli",
                "cli_command": "/usr/bin/echo",
            }
        }
    )


# ---------------------------------------------------------------------------
# __init__: stores config
# ---------------------------------------------------------------------------


def test_init_stores_config(http_config: ModelInferenceBridgeConfig) -> None:
    bridge = AdapterInferenceBridge(http_config)
    assert bridge._config is http_config


# ---------------------------------------------------------------------------
# infer(): unknown model_key
# ---------------------------------------------------------------------------


async def test_infer_unknown_model_key_raises(
    http_config: ModelInferenceBridgeConfig,
) -> None:
    bridge = AdapterInferenceBridge(http_config)
    with pytest.raises(ValueError, match="Unknown model_key"):
        await bridge.infer(
            model_key="does-not-exist",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# infer(): transport routing
# ---------------------------------------------------------------------------


async def test_infer_cli_transport_routes_to_call_cli_model(
    cli_config: ModelInferenceBridgeConfig,
) -> None:
    bridge = AdapterInferenceBridge(cli_config)
    with patch.object(
        bridge, "_call_cli_model", new=AsyncMock(return_value="cli-out")
    ) as mock_cli:
        result = await bridge.infer(
            model_key="test-cli",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )
    assert result == "cli-out"
    mock_cli.assert_awaited_once()


async def test_infer_http_transport_routes_to_call_http_model(
    http_config: ModelInferenceBridgeConfig,
) -> None:
    bridge = AdapterInferenceBridge(http_config)
    with patch.object(
        bridge, "_call_http_model", new=AsyncMock(return_value="http-out")
    ) as mock_http:
        result = await bridge.infer(
            model_key="test-http",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
            temperature=0.5,
        )
    assert result == "http-out"
    mock_http.assert_awaited_once()


async def test_infer_missing_transport_defaults_to_http() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "no-transport": {
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                # no "transport" key — defaults to "http"
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    with patch.object(
        bridge, "_call_http_model", new=AsyncMock(return_value="default-http")
    ) as mock_http:
        result = await bridge.infer(
            model_key="no-transport",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )
    assert result == "default-http"
    mock_http.assert_awaited_once()


# ---------------------------------------------------------------------------
# _call_http_model(): missing base_url
# ---------------------------------------------------------------------------


async def test_call_http_model_no_base_url_raises() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "no-url": {
                "transport": "http",
                "model_id": "gpt-test",
                # no base_url, no base_url_env
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    with pytest.raises(ValueError, match="missing base_url"):
        await bridge.infer(
            model_key="no-url",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


async def test_call_http_model_base_url_env_not_set_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIMARKET_TEST_MISSING_URL", raising=False)
    config = ModelInferenceBridgeConfig(
        model_configs={
            "env-url-missing": {
                "transport": "http",
                "model_id": "gpt-test",
                "base_url_env": "OMNIMARKET_TEST_MISSING_URL",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    with pytest.raises(ValueError, match="missing base_url"):
        await bridge.infer(
            model_key="env-url-missing",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


async def test_call_http_model_resolves_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIMARKET_TEST_BASE_URL", "https://resolved.example.com")
    config = ModelInferenceBridgeConfig(
        model_configs={
            "env-url": {
                "transport": "http",
                "model_id": "gpt-test",
                "base_url_env": "OMNIMARKET_TEST_BASE_URL",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, _ = _make_mock_httpx_cm("env-resolved")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="env-url",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )
    assert result == "env-resolved"


# ---------------------------------------------------------------------------
# _call_http_model(): missing model_id
# ---------------------------------------------------------------------------


async def test_call_http_model_no_model_id_raises() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "no-model-id": {
                "transport": "http",
                "base_url": "https://api.example.com",
                # no model_id
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    with pytest.raises(ValueError, match="missing model_id"):
        await bridge.infer(
            model_key="no-model-id",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# _call_http_model(): api_key → Authorization header
# ---------------------------------------------------------------------------


async def test_call_http_model_api_key_adds_authorization_header() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "with-key": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                "api_key": "sk-secret",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("authorized")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="with-key",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "authorized"
    call_kwargs = mock_client.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]
    assert headers.get("Authorization") == "Bearer sk-secret"


async def test_call_http_model_no_api_key_omits_authorization_header() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "no-key": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                # no api_key
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("no-auth")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        await bridge.infer(
            model_key="no-key",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    call_kwargs = mock_client.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# _call_http_model(): reserved extra_headers → warning + skip
# ---------------------------------------------------------------------------


async def test_call_http_model_reserved_extra_headers_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "reserved-header": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                "extra_headers": {
                    "authorization": "should-be-skipped",
                    "content-type": "also-skipped",
                    "X-Custom": "kept",
                },
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("ok")

    with (
        patch(_HTTP_MODULE, return_value=mock_cm),
        caplog.at_level(logging.WARNING, logger=_BRIDGE_LOGGER),
    ):
        result = await bridge.infer(
            model_key="reserved-header",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "ok"

    # Both reserved header keys must produce a warning
    warning_texts = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("authorization" in m.lower() for m in warning_texts)
    assert any("content-type" in m.lower() for m in warning_texts)

    # Non-reserved header passes through; reserved ones do not override
    call_kwargs = mock_client.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]
    assert headers.get("X-Custom") == "kept"
    assert headers.get("authorization") != "should-be-skipped"


# ---------------------------------------------------------------------------
# _call_http_model(): temperature from argument vs from config
# ---------------------------------------------------------------------------


async def test_call_http_model_temperature_from_argument_overrides_config() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "temp-test": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                "temperature": 0.9,
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    captured: dict[str, object] = {}

    async def fake_post(url: str, **kwargs: object) -> MagicMock:
        captured.update(kwargs.get("json", {}))  # type: ignore[arg-type]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "t-from-arg"}}]}
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="temp-test",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
            temperature=0.1,  # overrides cfg's 0.9
        )

    assert result == "t-from-arg"
    assert captured["temperature"] == pytest.approx(0.1)


async def test_call_http_model_temperature_falls_back_to_config() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "temp-from-cfg": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
                "temperature": 0.7,
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    captured: dict[str, object] = {}

    async def fake_post(url: str, **kwargs: object) -> MagicMock:
        captured.update(kwargs.get("json", {}))  # type: ignore[arg-type]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "t-from-cfg"}}]}
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="temp-from-cfg",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
            # temperature=None → falls back to cfg's 0.7
        )

    assert result == "t-from-cfg"
    assert captured["temperature"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# _call_http_model(): successful POST parses choices[0].message.content
# ---------------------------------------------------------------------------


async def test_call_http_model_success_parses_content() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "success": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "gpt-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, _ = _make_mock_httpx_cm("Hello, world!")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="success",
            system_prompt="system prompt",
            user_prompt="user prompt",
            timeout_seconds=10.0,
        )

    assert result == "Hello, world!"


# ---------------------------------------------------------------------------
# _call_cli_model(): missing cli_command
# ---------------------------------------------------------------------------


async def test_call_cli_model_missing_cli_command_raises() -> None:
    config = ModelInferenceBridgeConfig(
        model_configs={
            "cli-no-cmd": {
                "transport": "cli",
                # no cli_command
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    with pytest.raises(ValueError, match="missing cli_command"):
        await bridge.infer(
            model_key="cli-no-cmd",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# _call_cli_model(): asyncio.create_subprocess_exec → decoded stdout.strip()
# ---------------------------------------------------------------------------


def _cli_bridge(cli_command: str = "/usr/bin/echo") -> AdapterInferenceBridge:
    return AdapterInferenceBridge(
        ModelInferenceBridgeConfig(
            model_configs={
                "cli-echo": {
                    "transport": "cli",
                    "cli_command": cli_command,
                }
            }
        )
    )


async def test_call_cli_model_returns_stripped_stdout() -> None:
    bridge = _cli_bridge()
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"  hello from cli  \n", b""))
    mock_create = AsyncMock(return_value=mock_proc)

    with patch(_CREATE_SUBPROCESS, new=mock_create) as mock_exec:
        result = await bridge.infer(
            model_key="cli-echo",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "hello from cli"
    mock_exec.assert_awaited_once()
    mock_proc.communicate.assert_awaited_once()
    call_args = mock_exec.call_args.args
    assert call_args[0] == "/usr/bin/echo"
    assert "sys\n\nusr" in call_args[1]
    # PIPEs are wired so communicate() actually captures output.
    assert mock_exec.call_args.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert mock_exec.call_args.kwargs["stderr"] == asyncio.subprocess.PIPE


async def test_call_cli_model_combines_system_and_user_prompt() -> None:
    bridge = _cli_bridge("/usr/bin/mymodel")
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"combined", b""))
    mock_create = AsyncMock(return_value=mock_proc)

    with patch(_CREATE_SUBPROCESS, new=mock_create) as mock_exec:
        await bridge.infer(
            model_key="cli-echo",
            system_prompt="SYSTEM",
            user_prompt="USER",
            timeout_seconds=3.0,
        )

    call_args = mock_exec.call_args.args
    assert call_args[1] == "SYSTEM\n\nUSER"


async def test_call_cli_model_does_not_block_event_loop() -> None:
    """The CLI subprocess is awaited; the event loop keeps scheduling siblings.

    A blocking ``subprocess.run`` would freeze the single-threaded loop, the
    sibling ``setter`` coroutine would never run, and ``proc_started.wait()``
    would time out instead of returning. The test passing therefore proves the
    call yields control to the loop while the subprocess is in flight.
    """
    bridge = _cli_bridge()
    proc_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_communicate(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        proc_started.set()
        await asyncio.wait_for(release.wait(), timeout=1.0)
        return (b"async-out", b"")

    mock_proc = MagicMock()
    mock_proc.communicate = fake_communicate

    async def fake_create(*_a: object, **_k: object) -> MagicMock:
        return mock_proc

    async def setter() -> None:
        # Only reachable if the loop is not blocked while infer() awaits.
        await asyncio.wait_for(proc_started.wait(), timeout=1.0)
        release.set()

    with patch(_CREATE_SUBPROCESS, new=fake_create):
        infer_result, _ = await asyncio.gather(
            bridge.infer(
                model_key="cli-echo",
                system_prompt="sys",
                user_prompt="usr",
                timeout_seconds=5.0,
            ),
            setter(),
        )

    assert infer_result == "async-out"


# ---------------------------------------------------------------------------
# _call_http_model(): OMN-15152 fail-open defect cluster
#
# 1. base_url already carrying a /v1 suffix must not be double-appended to
#    ".../v1/v1/chat/completions" (the live-reproduced 404 repro).
# 2. reasoning-shape responses (message.reasoning populated, content empty)
#    must not crash with a bare, undiagnosable KeyError('content').
# ---------------------------------------------------------------------------


async def test_call_http_model_base_url_with_v1_suffix_does_not_double() -> None:
    """OMN-15152 repro #1: base_url already ends with /v1 -> must call
    <base_url>/chat/completions, never <base_url>/v1/chat/completions."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "v1-suffixed": {
                "transport": "http",
                "base_url": "http://localhost:11434/v1",
                "model_id": "gpt-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("ok")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="v1-suffixed",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "ok"
    call_args = mock_client.post.call_args
    url = call_args.args[0]
    assert url == "http://localhost:11434/v1/chat/completions"
    assert "/v1/v1/" not in url


async def test_call_http_model_base_url_with_trailing_slash_v1_does_not_double() -> (
    None
):
    """Same defect with a trailing slash on top of the /v1 suffix."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "v1-slash": {
                "transport": "http",
                "base_url": "http://localhost:11434/v1/",
                "model_id": "gpt-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("ok")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        await bridge.infer(
            model_key="v1-slash",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    url = mock_client.post.call_args.args[0]
    assert url == "http://localhost:11434/v1/chat/completions"


async def test_call_http_model_base_url_without_v1_still_appends_v1() -> None:
    """Existing behavior for a bare base_url (no /v1 suffix) is unchanged."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "no-v1": {
                "transport": "http",
                "base_url": "http://localhost:11434",
                "model_id": "gpt-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_cm, mock_client = _make_mock_httpx_cm("ok")

    with patch(_HTTP_MODULE, return_value=mock_cm):
        await bridge.infer(
            model_key="no-v1",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    url = mock_client.post.call_args.args[0]
    assert url == "http://localhost:11434/v1/chat/completions"


async def test_call_http_model_reasoning_shape_uses_reasoning_when_content_empty() -> (
    None
):
    """OMN-15152 repro #2: reasoning-model responses land text in
    message.reasoning with content empty/absent. Must not crash with a bare
    KeyError('content') and must surface the reasoning text."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "reasoning-model": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "reasoning-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning": "the actual model output landed here",
                }
            }
        ]
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="reasoning-model",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "the actual model output landed here"


async def test_call_http_model_content_absent_falls_back_to_reasoning() -> None:
    """Same as above but the content key is entirely absent, not just empty."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "reasoning-model-2": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "reasoning-test-2",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"reasoning": "reasoning-only text"}}]
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(_HTTP_MODULE, return_value=mock_cm):
        result = await bridge.infer(
            model_key="reasoning-model-2",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )

    assert result == "reasoning-only text"


async def test_call_http_model_no_content_or_reasoning_raises_clear_error() -> None:
    """Neither content nor reasoning present -> a specific, diagnosable
    ValueError, never a bare unhandled KeyError('content')."""
    config = ModelInferenceBridgeConfig(
        model_configs={
            "empty-model": {
                "transport": "http",
                "base_url": "https://api.example.com",
                "model_id": "empty-test",
            }
        }
    )
    bridge = AdapterInferenceBridge(config)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {}}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(_HTTP_MODULE, return_value=mock_cm),
        pytest.raises(ValueError, match="no content or reasoning"),
    ):
        await bridge.infer(
            model_key="empty-model",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=5.0,
        )


async def test_call_cli_model_timeout_maps_to_timeout_expired() -> None:
    bridge = _cli_bridge()

    async def slow_communicate(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return (b"", b"")

    mock_proc = MagicMock()
    mock_proc.communicate = slow_communicate
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_create = AsyncMock(return_value=mock_proc)

    with (
        patch(_CREATE_SUBPROCESS, new=mock_create),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        await bridge.infer(
            model_key="cli-echo",
            system_prompt="sys",
            user_prompt="usr",
            timeout_seconds=0.05,
        )

    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_awaited_once()
