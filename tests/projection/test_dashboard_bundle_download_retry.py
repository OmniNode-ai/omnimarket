# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dashboard bundle download recovers from gateway failures and keeps its refusal bounds."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

import pytest

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_local_dashboard_serve_effect import bundle

pytestmark = pytest.mark.unit

_URL = "https://github.com/OmniNode-ai/omnidash/releases/download/v1.1.11/bundle.tar.gz"


def _refused(status: int | None) -> GitHubApiError:
    return GitHubApiError("asset refused", status_code=status)


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("failures", [1, 2])
def test_gateway_failure_recovers_before_the_bundle_is_verified(
    status: int, failures: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = Mock(side_effect=[*(_refused(status) for _ in range(failures)), None])
    sleep = Mock()
    monkeypatch.setattr(bundle, "download_release_asset", download)
    monkeypatch.setattr(bundle.time, "sleep", sleep)
    destination = tmp_path / "bundle.tar.gz"

    bundle._download(_URL, destination)

    assert download.call_args_list == [call(_URL, destination)] * (failures + 1)
    assert sleep.call_args_list == [call(0.5 * 2**i) for i in range(failures)]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_persistent_gateway_failure_stops_after_three_attempts(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _refused(status)
    download = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(bundle, "download_release_asset", download)
    monkeypatch.setattr(bundle.time, "sleep", sleep)

    with pytest.raises(bundle.DashboardBundleError, match=_URL) as refused:
        bundle._download(_URL, tmp_path / "bundle.tar.gz")

    assert refused.value.__cause__ is error
    assert download.call_count == 3
    assert sleep.call_args_list == [call(0.5), call(1.0)]


@pytest.mark.parametrize("status", [None, 400, 401, 403, 404, 429, 500, 501])
def test_other_failures_are_not_retried(
    status: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = Mock(side_effect=_refused(status))
    sleep = Mock()
    monkeypatch.setattr(bundle, "download_release_asset", download)
    monkeypatch.setattr(bundle.time, "sleep", sleep)

    with pytest.raises(bundle.DashboardBundleError):
        bundle._download(_URL, tmp_path / "bundle.tar.gz")

    assert download.call_count == 1
    sleep.assert_not_called()
