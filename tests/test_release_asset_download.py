# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release downloads recover from transient gateways with a bounded attempt count."""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket import github_api

_URL = "https://github.com/example/project/releases/download/v1/bundle.tar.gz"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_failure_retries_and_writes_the_successful_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    failed_body = io.BytesIO(b"gateway failure")
    failure = urllib.error.HTTPError(_URL, status, "gateway", {}, failed_body)
    opening = Mock(side_effect=[failure, io.BytesIO(b"release bytes")])
    sleeping = Mock()
    monkeypatch.setattr(github_api.urllib.request, "urlopen", opening)
    monkeypatch.setattr("time.sleep", sleeping)
    target = tmp_path / "nested" / "asset"

    github_api.download_release_asset(_URL, target)

    assert target.read_bytes() == b"release bytes"
    assert failed_body.closed
    assert opening.call_count == 2
    assert sleeping.call_args_list == [((1.0,), {})]
    request = opening.call_args.args[0]
    assert request.full_url == _URL
    assert request.method == "GET"
    assert not request.has_header("Authorization")


def test_gateway_retries_stop_and_preserve_an_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures = [
        urllib.error.HTTPError(_URL, 504, "gateway", {}, io.BytesIO()) for _ in range(3)
    ]
    opening = Mock(side_effect=failures)
    sleeping = Mock()
    monkeypatch.setattr(github_api.urllib.request, "urlopen", opening)
    monkeypatch.setattr("time.sleep", sleeping)
    target = tmp_path / "asset"
    target.write_bytes(b"existing")

    with pytest.raises(github_api.GitHubApiError) as caught:
        github_api.download_release_asset(_URL, target)

    assert caught.value.status_code == 504
    assert caught.value.__cause__ is failures[-1]
    assert opening.call_count == 3
    assert sleeping.call_args_list == [((1.0,), {}), ((2.0,), {})]
    assert target.read_bytes() == b"existing"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_other_http_errors_fail_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    opening = Mock(
        side_effect=urllib.error.HTTPError(_URL, status, "refused", {}, io.BytesIO())
    )
    sleeping = Mock()
    monkeypatch.setattr(github_api.urllib.request, "urlopen", opening)
    monkeypatch.setattr("time.sleep", sleeping)

    with pytest.raises(github_api.GitHubApiError) as caught:
        github_api.download_release_asset(_URL, tmp_path / "asset")

    assert caught.value.status_code == status
    opening.assert_called_once()
    sleeping.assert_not_called()
    assert not (tmp_path / "asset").exists()


def test_non_https_asset_is_refused_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opening = Mock()
    monkeypatch.setattr(github_api.urllib.request, "urlopen", opening)

    with pytest.raises(github_api.GitHubApiError, match="non-https"):
        github_api.download_release_asset(
            "http://example.com/asset", tmp_path / "asset"
        )

    opening.assert_not_called()
