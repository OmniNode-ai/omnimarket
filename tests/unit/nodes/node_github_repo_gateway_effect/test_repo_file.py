# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's ``repo_file`` read: one file's text at one commit (OMN-24187).

The exchanges follow GitHub's documented contents API shape, replayed strictly:
a request that differs from the recorded one fails the replay. One run proves
the golden read, the not-found answer (a 404 is a result, not a fault), and the
error chain: an HTTP 502 and a directory answer are refused, and a request that
names no commit, no path, or a path outside the repository is refused before any
read.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from omnimarket.models.github_landing.model_transport_error import GitHubTransportError
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelRepoFileResult,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
)

pytestmark = pytest.mark.unit

_SHA = "a" * 40
_PATH = "contracts/OMN-1.yaml"
_URL = f"/repos/{REPO}/contents/{_PATH}?ref={_SHA}"


def _request(**kw: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.REPO_FILE,
            "repo": REPO,
            "head_sha": _SHA,
            "file_path": _PATH,
            **kw,
        }
    )


def _file_body(text: str) -> dict[str, object]:
    encoded = base64.b64encode(text.encode()).decode()
    # The contents API wraps its base64 in newlines every 60 characters.
    wrapped = "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60)) + "\n"
    return {"type": "file", "encoding": "base64", "content": wrapped}


def _run(exchange: JsonExchange) -> ModelRepoFileResult:
    transport = RecordedGatewayTransport([exchange])
    result = dispatch(_request(), transport)
    transport.assert_drained()
    assert isinstance(result, ModelRepoFileResult)
    return result


def test_repo_file_returns_the_decoded_text_at_the_commit() -> None:
    text = "ticket_id: OMN-1\n" + "dod_evidence: []\n" * 8
    result = _run(JsonExchange(get(_URL), json_response(_file_body(text))))
    assert result.found is True
    assert result.text == text
    assert (result.repo, result.head_sha, result.file_path) == (REPO, _SHA, _PATH)


def test_repo_file_answers_not_found_on_a_404() -> None:
    result = _run(JsonExchange(get(_URL), json_response({}, status=404)))
    assert result.found is False
    assert result.text is None


def test_repo_file_refuses_an_http_502() -> None:
    transport = RecordedGatewayTransport(
        [JsonExchange(get(_URL), json_response({}, status=502))]
    )
    with pytest.raises(GitHubTransportError, match="502"):
        dispatch(_request(), transport)


def test_repo_file_refuses_a_directory_answer() -> None:
    transport = RecordedGatewayTransport(
        [JsonExchange(get(_URL), json_response([{"name": "a", "type": "file"}]))]
    )
    with pytest.raises(ValueError, match=_PATH):
        dispatch(_request(), transport)


def test_repo_file_refuses_a_non_utf8_file() -> None:
    body = {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(b"\xff\xfe\x00").decode(),
    }
    transport = RecordedGatewayTransport([JsonExchange(get(_URL), json_response(body))])
    with pytest.raises(ValueError, match="UTF-8"):
        dispatch(_request(), transport)


@pytest.mark.parametrize(
    "override",
    [
        {"file_path": None},
        {"head_sha": None},
        {"file_path": "../x"},
        {"file_path": "/etc/passwd"},
        {"operation": EnumGithubGatewayOperation.MERGED_PR, "pr_number": 1},
    ],
)
def test_repo_file_request_refuses_a_shape_that_names_no_file(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _request(**override)
