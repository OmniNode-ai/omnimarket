# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The release download outlasts the edge's cached gateway failure (OMN-14172).

Recorded failure: on 2026-11-08 the integration silent-skip guard job failed on
sixteen pull requests with ``HTTP Error 504: Gateway Time-out`` from
``tests/projection/test_dashboard_bundle_pin_omn21469.py`` (job 128679367241,
job 128683982397). Re-run from a lab host the same afternoon, the release URL
answered a cold request with a 504 after about eleven seconds and then
answered every further request with a 504 in about a tenth of a second for
more than eighty seconds, until a later cold request succeeded. The download
helper's three attempts, spaced half a second and one second apart, all landed
inside that cached refusal.

The control replays that behaviour on a fake clock: a cold request fails and
caches the refusal for ``_CACHED_REFUSAL_SECONDS``; a request after the cache
has lapsed is served. The helper must recover without any real waiting.
"""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from omnimarket import github_api

pytestmark = pytest.mark.unit

_URL = "https://github.com/OmniNode-ai/omnidash/releases/download/v1.1.11/bundle.tar.gz"
# Longest cached refusal read on the lab host (cache still replaying at 81 s).
_CACHED_REFUSAL_SECONDS = 90.0


class _Edge:
    """A fake clock and an edge that caches a cold gateway failure."""

    def __init__(self, cached_refusal_seconds: float = _CACHED_REFUSAL_SECONDS) -> None:
        self.cached_refusal_seconds = cached_refusal_seconds
        self.now = 0.0
        self.cached_until: float | None = None
        self.requests = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def urlopen(self, request: object, timeout: float) -> io.BytesIO:
        self.requests += 1
        if self.cached_until is not None and self.now < self.cached_until:
            raise urllib.error.HTTPError(_URL, 504, "gateway", {}, io.BytesIO())
        if self.cached_until is None:
            self.cached_until = self.now + self.cached_refusal_seconds
            raise urllib.error.HTTPError(_URL, 504, "gateway", {}, io.BytesIO())
        return io.BytesIO(b"release bytes")


def test_download_recovers_after_the_edge_stops_replaying_a_cold_504(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = _Edge()
    monkeypatch.setattr(github_api.urllib.request, "urlopen", edge.urlopen)
    monkeypatch.setattr(github_api.time, "sleep", edge.sleep)
    target = tmp_path / "asset"

    github_api.download_release_asset(_URL, target)

    assert target.read_bytes() == b"release bytes"
    assert edge.now >= _CACHED_REFUSAL_SECONDS


def test_a_refusal_that_never_lapses_still_ends_in_the_gateway_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = _Edge(cached_refusal_seconds=10_000.0)
    monkeypatch.setattr(github_api.urllib.request, "urlopen", edge.urlopen)
    monkeypatch.setattr(github_api.time, "sleep", edge.sleep)

    with pytest.raises(github_api.GitHubApiError) as refused:
        github_api.download_release_asset(_URL, tmp_path / "asset")

    assert refused.value.status_code == 504
    assert not (tmp_path / "asset").exists()
