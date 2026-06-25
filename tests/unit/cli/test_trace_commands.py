# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for onex market trace commands.

All HTTP calls are mocked via unittest.mock.patch on httpx.Client.get.
No network, no DB.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from omnimarket.cli.market import market


@pytest.fixture(autouse=True)
def _set_omnidash_url(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Bind OMNIDASH_API_URL for all tests in this module.

    OMN-12807: _DEFAULT_BASE_URL removed from trace.py; the env var is now
    required.  Unit tests that mock httpx.Client still need the var set so
    _base_url() does not raise before the mock intercepts the request.
    """
    monkeypatch.setenv("OMNIDASH_API_URL", "http://test-projection-api:3002")
    return


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENTRY_A = {
    "entry_id": "eid-001",
    "timestamp": "2025-05-25T12:00:01.000Z",
    "node_name": "node_build_loop",
    "function_name": "handle",
    "level": "info",
    "message": "Phase transition started",
    "correlation_id": "corr-abc-123",
    "duration_ms": None,
    "metadata": {},
}

_ENTRY_B = {
    "entry_id": "eid-002",
    "timestamp": "2025-05-25T12:00:02.567Z",
    "node_name": "node_build_loop",
    "function_name": "handle",
    "level": "info",
    "message": "Running tests",
    "correlation_id": "corr-abc-123",
    "duration_ms": 1567.0,
    "metadata": {},
}

_ENTRY_C = {
    "entry_id": "eid-003",
    "timestamp": "2025-05-25T12:00:05.890Z",
    "node_name": "node_test_runner",
    "function_name": "run",
    "level": "error",
    "message": "Test failed: assertion error",
    "correlation_id": "corr-abc-123",
    "duration_ms": None,
    "metadata": {},
}

_ENTRY_D = {
    "entry_id": "eid-004",
    "timestamp": "2025-05-25T12:01:00.000Z",
    "node_name": "node_merge_sweep",
    "function_name": "sweep",
    "level": "info",
    "message": "Sweep complete",
    "correlation_id": "corr-xyz-999",
    "duration_ms": None,
    "metadata": {},
}

_PROJECTION_PAYLOAD: dict[str, Any] = {
    "rows": [_ENTRY_A, _ENTRY_B, _ENTRY_C, _ENTRY_D],
    "row_count": 4,
    "topic": "onex.evt.platform.log-entry.v1",
}

_PROJECTION_PAYLOAD_CORR_ABC: dict[str, Any] = {
    "rows": [_ENTRY_A, _ENTRY_B, _ENTRY_C],
    "row_count": 3,
    "topic": "onex.evt.platform.log-entry.v1",
}


def _make_mock_response(payload: dict[str, Any], status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _mock_client(payload: dict[str, Any]) -> MagicMock:
    mock_resp = _make_mock_response(payload)
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    return mock_client


# ---------------------------------------------------------------------------
# trace group registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_group_registered_on_market() -> None:
    assert "trace" in market.commands


@pytest.mark.unit
def test_trace_subcommands_registered() -> None:
    trace_group = market.commands["trace"]
    assert hasattr(trace_group, "commands")
    assert "list" in trace_group.commands
    assert "query" in trace_group.commands
    assert "watch" in trace_group.commands


# ---------------------------------------------------------------------------
# trace list — text output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_list_shows_correlation_ids() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list"])
    assert result.exit_code == 0, result.output
    assert "corr-abc-123" in result.output
    assert "corr-xyz-999" in result.output


@pytest.mark.unit
def test_trace_list_shows_event_counts() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list"])
    assert result.exit_code == 0
    # corr-abc-123 has 3 entries
    assert "3" in result.output


@pytest.mark.unit
def test_trace_list_json_format() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    cids = {item["correlation_id"] for item in data}
    assert "corr-abc-123" in cids
    assert "corr-xyz-999" in cids


@pytest.mark.unit
def test_trace_list_json_contains_summary_fields() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    item = next(i for i in data if i["correlation_id"] == "corr-abc-123")
    assert "node_count" in item
    assert "event_count" in item
    assert "duration" in item
    assert "status" in item
    assert "latest_message" in item


@pytest.mark.unit
def test_trace_list_error_status_when_error_entry() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    abc = next(i for i in data if i["correlation_id"] == "corr-abc-123")
    assert abc["status"] == "error"


@pytest.mark.unit
def test_trace_list_done_status_without_errors() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(market, ["trace", "list", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    xyz = next(i for i in data if i["correlation_id"] == "corr-xyz-999")
    assert xyz["status"] == "done"


@pytest.mark.unit
def test_trace_list_running_only_filters() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(
            market, ["trace", "list", "--running-only", "--format", "json"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    # None of our test entries have "running" status
    assert data == []


@pytest.mark.unit
def test_trace_list_since_filter_excludes_old_entries() -> None:
    runner = CliRunner()
    # Only entries at or after 12:01 should appear → only corr-xyz-999
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(
            market,
            [
                "trace",
                "list",
                "--since",
                "2025-05-25T12:01:00.000Z",
                "--format",
                "json",
            ],
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    cids = {i["correlation_id"] for i in data}
    assert "corr-xyz-999" in cids
    assert "corr-abc-123" not in cids


@pytest.mark.unit
def test_trace_list_no_results() -> None:
    runner = CliRunner()
    empty_payload: dict[str, Any] = {"rows": [], "row_count": 0}
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(empty_payload),
    ):
        result = runner.invoke(market, ["trace", "list"])
    assert result.exit_code == 0
    assert "No traces found." in result.output


@pytest.mark.unit
def test_trace_list_limit_applied() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD),
    ):
        result = runner.invoke(
            market, ["trace", "list", "--limit", "1", "--format", "json"]
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1


# ---------------------------------------------------------------------------
# trace query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_query_renders_timeline() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market, ["trace", "query", "--correlation-id", "corr-abc-123"]
        )
    assert result.exit_code == 0, result.output
    assert "Phase transition started" in result.output
    assert "Running tests" in result.output
    assert "Test failed" in result.output


@pytest.mark.unit
def test_trace_query_renders_node_names() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market, ["trace", "query", "--correlation-id", "corr-abc-123"]
        )
    assert result.exit_code == 0
    assert "node_build_loop" in result.output
    assert "node_test_runner" in result.output


@pytest.mark.unit
def test_trace_query_json_format() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market,
            ["trace", "query", "--correlation-id", "corr-abc-123", "--format", "json"],
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 3
    assert data[0]["node_name"] == "node_build_loop"


@pytest.mark.unit
def test_trace_query_yaml_format() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market,
            ["trace", "query", "--correlation-id", "corr-abc-123", "--format", "yaml"],
        )
    assert result.exit_code == 0
    assert "node_name: node_build_loop" in result.output


@pytest.mark.unit
def test_trace_query_no_results_message() -> None:
    runner = CliRunner()
    empty: dict[str, Any] = {"rows": [], "row_count": 0}
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client", return_value=_mock_client(empty)
    ):
        result = runner.invoke(
            market, ["trace", "query", "--correlation-id", "missing-id"]
        )
    assert result.exit_code == 0
    assert "No events found" in result.output


@pytest.mark.unit
def test_trace_query_since_filter() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market,
            [
                "trace",
                "query",
                "--correlation-id",
                "corr-abc-123",
                "--since",
                "2025-05-25T12:00:05.000Z",
                "--format",
                "json",
            ],
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    # Only eid-003 is at or after 12:00:05
    assert len(data) == 1
    assert data[0]["entry_id"] == "eid-003"


@pytest.mark.unit
def test_trace_query_limit_applied() -> None:
    runner = CliRunner()
    with patch(
        "omnimarket.cli.commands.trace.httpx.Client",
        return_value=_mock_client(_PROJECTION_PAYLOAD_CORR_ABC),
    ):
        result = runner.invoke(
            market,
            [
                "trace",
                "query",
                "--correlation-id",
                "corr-abc-123",
                "--limit",
                "1",
                "--format",
                "json",
            ],
        )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1


@pytest.mark.unit
def test_trace_query_requires_correlation_id() -> None:
    runner = CliRunner()
    result = runner.invoke(market, ["trace", "query"])
    assert result.exit_code != 0
    assert "correlation-id" in result.output.lower() or "Missing" in result.output


# ---------------------------------------------------------------------------
# trace watch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_watch_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(market, ["trace", "watch", "--help"])
    assert result.exit_code == 0
    assert "--correlation-id" in result.output
    assert "--node" in result.output
    assert "--level" in result.output
    assert "--interval" in result.output


@pytest.mark.unit
def test_trace_watch_exits_on_keyboard_interrupt() -> None:
    """Verify watch handles KeyboardInterrupt gracefully."""
    import omnimarket.cli.commands.trace as trace_mod

    runner = CliRunner()

    call_count = 0

    def raising_fetch(
        client: Any, *, topic: str, correlation_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise KeyboardInterrupt

    with (
        patch.object(trace_mod, "_fetch_projection", side_effect=raising_fetch),
        patch("omnimarket.cli.commands.trace.time.sleep"),
    ):
        result = runner.invoke(market, ["trace", "watch", "--interval", "0.01"])

    assert result.exit_code == 0
    assert "Watch stopped" in result.output


@pytest.mark.unit
def test_trace_watch_deduplicates_entries() -> None:
    """Each entry_id appears only once even across multiple polls."""
    import omnimarket.cli.commands.trace as trace_mod

    runner = CliRunner()
    call_count = 0

    def fake_fetch(
        client: Any, *, topic: str, correlation_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"rows": [_ENTRY_A]}
        raise KeyboardInterrupt

    with (
        patch.object(trace_mod, "_fetch_projection", side_effect=fake_fetch),
        patch("omnimarket.cli.commands.trace.time.sleep"),
    ):
        result = runner.invoke(market, ["trace", "watch", "--interval", "0.01"])

    assert result.exit_code == 0
    # "Phase transition started" appears exactly once
    assert result.output.count("Phase transition started") == 1


@pytest.mark.unit
def test_trace_watch_node_filter() -> None:
    """--node filters entries to only matching node_name."""
    import omnimarket.cli.commands.trace as trace_mod

    runner = CliRunner()
    call_count = 0

    def fake_fetch(
        client: Any, *, topic: str, correlation_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"rows": [_ENTRY_A, _ENTRY_C]}
        raise KeyboardInterrupt

    with (
        patch.object(trace_mod, "_fetch_projection", side_effect=fake_fetch),
        patch("omnimarket.cli.commands.trace.time.sleep"),
    ):
        result = runner.invoke(
            market,
            ["trace", "watch", "--node", "node_test_runner", "--interval", "0.01"],
        )

    assert result.exit_code == 0
    assert "Test failed" in result.output
    assert "Phase transition" not in result.output


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_list_api_error_shows_message() -> None:
    import httpx

    runner = CliRunner()
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service unavailable"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=MagicMock(), response=mock_resp
    )
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("omnimarket.cli.commands.trace.httpx.Client", return_value=mock_client):
        result = runner.invoke(market, ["trace", "list"])

    assert result.exit_code != 0
    assert "503" in result.output or "Projection API error" in result.output


@pytest.mark.unit
def test_trace_list_connection_error_shows_message() -> None:
    import httpx

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch("omnimarket.cli.commands.trace.httpx.Client", return_value=mock_client):
        result = runner.invoke(market, ["trace", "list"])

    assert result.exit_code != 0
    assert (
        "projection API" in result.output.lower() or "Could not reach" in result.output
    )


# ---------------------------------------------------------------------------
# Base URL configuration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_base_url_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnimarket.cli.commands.trace as trace_mod

    monkeypatch.setenv("OMNIDASH_API_URL", "http://myserver:9999")
    assert trace_mod._base_url() == "http://myserver:9999"


@pytest.mark.unit
def test_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnimarket.cli.commands.trace as trace_mod

    monkeypatch.setenv("OMNIDASH_API_URL", "http://myserver:9999/")
    assert trace_mod._base_url() == "http://myserver:9999"


@pytest.mark.unit
def test_base_url_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """_base_url() fails closed when OMNIDASH_API_URL is unset (no localhost default).

    OMN-12807: _DEFAULT_BASE_URL removed; callers must bind OMNIDASH_API_URL
    to the projection-API address for the active lane.
    """
    import click

    import omnimarket.cli.commands.trace as trace_mod

    monkeypatch.delenv("OMNIDASH_API_URL", raising=False)
    with pytest.raises(click.ClickException, match="OMNIDASH_API_URL is not set"):
        trace_mod._base_url()
