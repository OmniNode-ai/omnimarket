# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the checksum-sidecar HTTP 504 recorded by the integration job."""

from __future__ import annotations

import io
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omnimarket.github_api import GitHubApiError, download_release_asset

pytestmark = pytest.mark.unit
URL = "https://example.invalid/releases/bundle.tar.gz.sha256"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_failure_then_success_downloads_the_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls: list[urllib.request.Request] = []
    delays: list[float] = []

    def open_asset(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        calls.append(request)
        assert timeout == 30.0
        if len(calls) == 1:
            raise urllib.error.HTTPError(URL, status, "Gateway failure", {}, None)
        return io.BytesIO(b"digest  bundle.tar.gz\n")

    monkeypatch.setattr(urllib.request, "urlopen", open_asset)
    monkeypatch.setattr(time, "sleep", delays.append)
    destination = tmp_path / "assets" / "bundle.sha256"
    download_release_asset(URL, destination)
    assert destination.read_bytes() == b"digest  bundle.tar.gz\n"
    assert len(calls) == 2
    assert delays == [1.0]
    assert all(request.get_method() == "GET" for request in calls)
    assert all(not request.has_header("Authorization") for request in calls)


@pytest.mark.parametrize(
    ("status", "attempts"), [(504, 3), (403, 1), (404, 1), (429, 1)]
)
def test_persistent_failure_preserves_the_status_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, attempts: int
) -> None:
    calls: list[str] = []
    delays: list[float] = []

    def open_asset(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        calls.append(request.full_url)
        raise urllib.error.HTTPError(URL, status, "asset unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", open_asset)
    monkeypatch.setattr(time, "sleep", delays.append)
    destination = tmp_path / "bundle.sha256"
    with pytest.raises(GitHubApiError) as caught:
        download_release_asset(URL, destination)
    assert caught.value.status_code == status
    assert len(calls) == attempts
    assert delays == [1.0, 2.0][: attempts - 1]
    assert not destination.exists()


def test_a_plain_http_url_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_request(*args: object, **kwargs: object) -> None:
        pytest.fail("an insecure URL reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_request)
    with pytest.raises(GitHubApiError, match="non-https"):
        download_release_asset("http://example.invalid/asset", tmp_path / "asset")
