"""Unit tests for TargetHttp — mocked urllib, no network access."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.targets.http_check_target import (
    TargetHttp,
)


def _target(**overrides: object) -> TargetHttp:
    defaults = {
        "name": "test-endpoint",
        "url": "http://localhost:8000/health",
        "timeout": 5.0,
        "category": EnumCheckTarget.LLM_ENDPOINTS,
    }
    defaults.update(overrides)
    return TargetHttp(**defaults)


@pytest.mark.unit
class TestTargetHttpValidation:
    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            _target(name="")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ValueError, match="url"):
            _target(url="")

    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _target(timeout=0)

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _target(timeout=-1)


@pytest.mark.unit
class TestTargetHttpCheck:
    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen"
    )
    def test_healthy_on_200(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _target().check()
        assert result.status == EnumCheckStatus.HEALTHY
        assert result.category == EnumCheckTarget.LLM_ENDPOINTS

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen"
    )
    def test_degraded_on_non_2xx(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _target().check()
        assert result.status == EnumCheckStatus.DEGRADED
        assert result.details["status_code"] == 503

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen",
        side_effect=HTTPError(
            url="http://localhost:8000/health",
            code=503,
            msg="service unavailable",
            hdrs=None,
            fp=None,
        ),
    )
    def test_degraded_on_http_error(self, mock_urlopen: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DEGRADED
        assert result.details["status_code"] == 503

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen",
        side_effect=__import__("urllib.error").error.URLError("Connection refused"),
    )
    def test_down_on_connection_refused(self, mock_urlopen: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen",
        side_effect=TimeoutError("timed out"),
    )
    def test_down_on_timeout(self, mock_urlopen: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.DOWN
        assert "timed out" in result.message.lower()

    @patch(
        "omnimarket.nodes.node_process_watchdog.targets.http_check_target.urllib.request.urlopen",
        side_effect=RuntimeError("unexpected"),
    )
    def test_unknown_on_unexpected_error(self, mock_urlopen: MagicMock) -> None:
        result = _target().check()
        assert result.status == EnumCheckStatus.UNKNOWN

    def test_restart_returns_false(self) -> None:
        assert _target().restart() is False
