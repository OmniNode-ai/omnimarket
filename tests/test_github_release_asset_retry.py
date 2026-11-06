# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public asset reads recover from gateway failures and retain refusal bounds."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from omnimarket import github_api

pytestmark = pytest.mark.unit

_URL = "https://github.com/OmniNode-ai/omnidash/releases/download/v1.1.11/bundle.tar.gz"


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(_URL, status, "asset refused", Message(), None)


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("failures", [1, 2])
def test_gateway_failure_recovers_before_writing_asset(
    status: int, failures: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_url = Mock(
        side_effect=[
            *(_http_error(status) for _ in range(failures)),
            io.BytesIO(b"verified later"),
        ]
    )
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)
    sleep = Mock()
    monkeypatch.setattr("omnimarket.github_api.time.sleep", sleep)
    destination = tmp_path / "cache" / "bundle.tar.gz"

    github_api.download_release_asset(_URL, destination)

    assert destination.read_bytes() == b"verified later"
    assert open_url.call_count == failures + 1
    assert sleep.call_args_list == [call(0.5 * 2**i) for i in range(failures)]
    for request_call in open_url.call_args_list:
        assert request_call.args[0].get_method() == "GET"
        assert request_call.args[0].full_url == _URL
        assert request_call.kwargs == {"timeout": 30.0}


@pytest.mark.parametrize("status", [502, 503, 504])
def test_persistent_gateway_failure_stops_after_three_attempts(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _http_error(status)
    open_url = Mock(side_effect=error)
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)
    sleep = Mock()
    monkeypatch.setattr("omnimarket.github_api.time.sleep", sleep)
    destination = tmp_path / "bundle.tar.gz"

    with pytest.raises(github_api.GitHubApiError) as refused:
        github_api.download_release_asset(_URL, destination)

    assert refused.value.status_code == status
    assert refused.value.__cause__ is error
    assert open_url.call_count == 3
    assert sleep.call_args_list == [call(0.5), call(1.0)]
    assert not destination.exists()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 501])
def test_other_http_failures_are_not_retried(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_url = Mock(side_effect=_http_error(status))
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)

    with pytest.raises(github_api.GitHubApiError) as refused:
        github_api.download_release_asset(_URL, tmp_path / "bundle.tar.gz")

    assert refused.value.status_code == status
    assert open_url.call_count == 1


def test_network_errors_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_url = Mock(side_effect=urllib.error.URLError("connection refused"))
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)

    with pytest.raises(github_api.GitHubApiError, match="connection refused"):
        github_api.download_release_asset(_URL, tmp_path / "bundle.tar.gz")

    assert open_url.call_count == 1


def test_non_https_url_is_refused_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_url = Mock()
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)

    with pytest.raises(github_api.GitHubApiError, match="non-https"):
        github_api.download_release_asset(
            "http://example.com/asset", tmp_path / "asset"
        )

    open_url.assert_not_called()


def test_local_write_failure_does_not_download_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_url = Mock(return_value=io.BytesIO(b"asset"))
    monkeypatch.setattr("omnimarket.github_api.urllib.request.urlopen", open_url)
    write_bytes = Mock(side_effect=OSError("disk full"))
    monkeypatch.setattr(Path, "write_bytes", write_bytes)

    with pytest.raises(github_api.GitHubApiError, match="disk full"):
        github_api.download_release_asset(_URL, tmp_path / "asset")

    assert open_url.call_count == 1
    assert write_bytes.call_args_list == [call(b"asset")]
