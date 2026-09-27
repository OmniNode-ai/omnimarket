# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared GitHub landing transport (OMN-19831), with urlopen replaced.

No network: ``urllib.request.urlopen`` is patched to capture the built request
and answer, or raise, the way urllib does.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.config.service_endpoints import GITHUB_GRAPHQL_URL, GITHUB_REST_URL
from omnimarket.github_landing.github_landing_requests import (
    fast_forward_ref_request,
    head_check_runs_request,
    landing_policy_request,
    update_branch_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
    parse_response_body,
    request_url,
)

pytestmark = pytest.mark.unit

_TOKEN = "test-token-value-not-real"  # local-path-ok: fake credential


def _message(headers: dict[str, str]) -> Message:
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    return msg


class _Resp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = _message(headers)
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Capture:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(req)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


_QUOTA = {
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Remaining": "4000",
    "X-RateLimit-Used": "1000",
    "X-RateLimit-Reset": "1790465451",
    "X-RateLimit-Resource": "core",
}


def _transport() -> UrllibGithubLandingTransport:
    return UrllibGithubLandingTransport(SecretStr(_TOKEN))


def test_urls_come_from_the_service_endpoints() -> None:
    assert request_url(landing_policy_request("o/r", 1)) == GITHUB_GRAPHQL_URL
    rest = update_branch_request("o/r", 7, "a" * 40)
    assert (
        request_url(rest)
        == f"{GITHUB_REST_URL.rstrip('/')}/repos/o/r/pulls/7/update-branch"
    )


def test_a_2xx_answer_comes_back_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture(
        _Resp(202, _QUOTA, b'{"message": "Updating pull request branch."}')
    )
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    response = _transport().send_sync(update_branch_request("o/r", 7, "a" * 40))
    assert response.status == 202
    assert response.header("x-ratelimit-remaining") == "4000"
    assert response.body == {"message": "Updating pull request branch."}
    (sent,) = capture.requests
    assert sent.get_method() == "PUT"
    assert sent.get_header("Authorization") == f"Bearer {_TOKEN}"
    assert sent.get_header("Content-type") == "application/json"
    assert isinstance(sent.data, bytes)
    assert json.loads(sent.data) == {"expected_head_sha": "a" * 40}


def test_the_conditional_header_is_sent_and_a_304_is_a_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_modified = urllib.error.HTTPError(
        "https://example.invalid",
        304,
        "Not Modified",
        _message(_QUOTA),
        io.BytesIO(b""),
    )
    capture = _Capture(not_modified)
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    response = _transport().send_sync(
        head_check_runs_request("o/r", "b" * 40, etag='W/"e"')
    )
    assert response.status == 304
    assert response.body is None
    assert response.header("x-ratelimit-resource") == "core"
    assert capture.requests[0].get_header("If-none-match") == 'W/"e"'


def test_an_error_status_is_a_response_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"message": "You have exceeded a secondary rate limit."}'
    forbidden = urllib.error.HTTPError(
        "https://example.invalid",
        403,
        "Forbidden",
        _message({**_QUOTA, "Retry-After": "60"}),
        io.BytesIO(body),
    )
    monkeypatch.setattr(urllib.request, "urlopen", _Capture(forbidden))
    response = _transport().send_sync(update_branch_request("o/r", 7, "a" * 40))
    assert response.status == 403
    assert response.retry_after_seconds() == 60
    assert "secondary rate limit" in response.message()


def test_patch_is_sent_as_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture(_Resp(200, _QUOTA, b'{"ref": "refs/heads/x"}'))
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    _transport().send_sync(fast_forward_ref_request("o/r", "x", "c" * 40))
    assert capture.requests[0].get_method() == "PATCH"


def test_no_response_raises_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    down = urllib.error.URLError(f"connection refused while holding {_TOKEN}")
    monkeypatch.setattr(urllib.request, "urlopen", _Capture(down))
    with pytest.raises(GithubLandingTransportError) as caught:
        _transport().send_sync(update_branch_request("o/r", 7, "a" * 40))
    # The message names the request, and never the credential, even when the
    # underlying error text happens to contain it.
    assert "PUT /repos/o/r/pulls/7/update-branch" in str(caught.value)
    assert _TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        UrllibGithubLandingTransport(SecretStr(""))


async def test_the_async_send_uses_the_same_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _Capture(_Resp(200, _QUOTA, b'{"data": {}}'))
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    response = await _transport().send(landing_policy_request("o/r", 1))
    assert response.status == 200
    assert capture.requests[0].full_url == GITHUB_GRAPHQL_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", None),
        (b'{"a": 1}', {"a": 1}),
        (b"[1, 2]", {"value": [1, 2]}),
        (b"<html>bad gateway</html>", {"message": "<html>bad gateway</html>"}),
    ],
)
def test_bodies_are_parsed_into_the_response_shape(
    raw: bytes, expected: dict[str, object] | None
) -> None:
    assert parse_response_body(raw) == expected
