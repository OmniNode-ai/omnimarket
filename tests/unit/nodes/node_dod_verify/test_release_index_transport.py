# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release-index transport controls for the recorded indeterminate negative probe."""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as evidence,
)

pytestmark = pytest.mark.unit


def _missing() -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), urllib.error.URLError("reset"), OSError("reset")],
)
def test_transient_transport_failure_then_definite_missing_release(
    failure: Exception,
) -> None:
    # CI observed None for this negative control; it did not record which
    # transport exception occurred. Exercise each supported transport failure.
    with patch.object(
        evidence.urllib.request, "urlopen", side_effect=[failure, _missing()]
    ) as opened:
        assert (
            evidence.pypi_release_files("omnibase-core", "0.99.99", timeout_s=0.25)
            == frozenset()
        )
    url = evidence.PACKAGE_INDEX_JSON_URL.format(
        distribution="omnibase-core", version="0.99.99"
    )
    assert opened.call_args_list == [call(url, timeout=0.25), call(url, timeout=0.25)]


def test_transient_transport_failure_then_served_release() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = (
        b'{"urls":[{"packagetype":"bdist_wheel"},{"packagetype":"sdist"}]}'
    )
    with patch.object(
        evidence.urllib.request, "urlopen", side_effect=[TimeoutError(), response]
    ) as opened:
        assert evidence.pypi_release_files("omnibase-core", "0.47.4") == frozenset(
            {"bdist_wheel", "sdist"}
        )
    assert opened.call_count == 2


def test_transport_failure_exhaustion_stays_indeterminate() -> None:
    with patch.object(
        evidence.urllib.request, "urlopen", side_effect=TimeoutError()
    ) as opened:
        assert evidence.pypi_release_files("omnibase-core", "0.99.99") is None
    assert opened.call_count == 2


@pytest.mark.parametrize("status", [403, 404, 503])
def test_definite_http_answer_is_not_a_transport_retry(status: int) -> None:
    failure = urllib.error.HTTPError(
        "https://example.invalid", status, "answer", {}, None
    )
    with patch.object(
        evidence.urllib.request, "urlopen", side_effect=failure
    ) as opened:
        result = evidence.pypi_release_files("omnibase-core", "0.99.99")
    assert result == (frozenset() if status == 404 else None)
    assert opened.call_count == 1


@pytest.mark.parametrize("body", [b"not json", b'{"urls":null}'])
def test_invalid_index_payload_stays_indeterminate(body: bytes) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = body
    with patch.object(
        evidence.urllib.request, "urlopen", return_value=response
    ) as opened:
        assert evidence.pypi_release_files("omnibase-core", "0.99.99") is None
    assert opened.call_count == 1
