# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's ``open_pr_facts`` read: every open pull request with what the duplicate-fix guard folds over (OMN-22654).

The exchanges follow GitHub's documented REST list shape, replayed strictly: a request that differs from the recorded
one fails the replay. One run proves the result, the Fix-Check and Fix-Cause lines kept from a body that is not sent,
the cut flag at the request's limit, and the refusal of a pull request the read cannot shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelOpenPrFactsResult,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
    pull_request,
)

pytestmark = pytest.mark.unit

_BASE = f"/repos/{REPO}"
_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _request(**kw: object) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest.model_validate(
        {"operation": EnumGithubGatewayOperation.OPEN_PR_FACTS, "repo": REPO, **kw}
    )


def _open_pr(
    number: int, *, body: str, ref: str, sha: str, base: str = "dev"
) -> dict[str, object]:
    pr = pull_request(number, body=body)
    return {
        **pr,
        "title": f"fix(OMN-{number}): a change",
        "head": {"ref": ref, "sha": sha},
        "base": {"ref": base, "sha": "0" * 40},
    }


def _run(
    request: ModelGithubGatewayRequest, exchanges: list[JsonExchange]
) -> ModelOpenPrFactsResult:
    transport = RecordedGatewayTransport(list(exchanges))
    result = dispatch(request, transport)
    transport.assert_drained()
    assert isinstance(result, ModelOpenPrFactsResult)
    return result


def test_open_pr_facts_lists_each_open_pr_with_its_head_base_title_and_fix_markers() -> (
    None
):
    body = (
        "Why this change.\n"
        "Fix-Check: Lab-overlay parity tests (OMN-20934)\n"
        "More prose.\n"
        "fix-cause: OmniNode-ai/omnibase_infra:daa0c224fe09\n"
        "not a marker: Fix-Check: indented mid-line\n"
    )
    result = _run(
        _request(limit=50),
        [
            JsonExchange(
                get(f"{_BASE}/pulls?state=open&per_page=50"),
                json_response(
                    [
                        _open_pr(12, body=body, ref="jonah/omn-12-fix", sha=_SHA_A),
                        _open_pr(11, body="no markers", ref="jonah/omn-11", sha=_SHA_B),
                    ]
                ),
            )
        ],
    )
    assert [pr.number for pr in result.prs] == [12, 11]
    first, second = result.prs
    assert (first.head_ref, first.head_sha, first.base_ref) == (
        "jonah/omn-12-fix",
        _SHA_A,
        "dev",
    )
    assert first.title == "fix(OMN-12): a change"
    assert first.draft is False
    assert first.fix_markers == [
        "Fix-Check: Lab-overlay parity tests (OMN-20934)",
        "fix-cause: OmniNode-ai/omnibase_infra:daa0c224fe09",
    ]
    assert second.fix_markers == []
    assert result.truncated is False
    # The body itself never travels: the result model has no field for it.
    assert "body" not in type(first).model_fields


def test_open_pr_facts_says_when_it_cut_at_the_limit() -> None:
    result = _run(
        _request(limit=1),
        [
            JsonExchange(
                get(f"{_BASE}/pulls?state=open&per_page=1"),
                json_response(
                    [
                        _open_pr(12, body="", ref="a/b", sha=_SHA_A),
                        _open_pr(11, body="", ref="c/d", sha=_SHA_B),
                    ]
                ),
            )
        ],
    )
    assert [pr.number for pr in result.prs] == [12]
    assert result.truncated is True


def test_open_pr_facts_bounds_the_marker_lines_it_keeps() -> None:
    body = "\n".join(f"Fix-Check: check {n}" for n in range(30))
    result = _run(
        _request(limit=10),
        [
            JsonExchange(
                get(f"{_BASE}/pulls?state=open&per_page=10"),
                json_response([_open_pr(5, body=body, ref="a/b", sha=_SHA_A)]),
            )
        ],
    )
    assert len(result.prs[0].fix_markers) == 8


def test_open_pr_facts_refuses_a_pull_request_it_cannot_shape() -> None:
    """A PR with no head sha is an error: a list that dropped one would let the guard decide without it."""
    broken = _open_pr(7, body="", ref="a/b", sha=_SHA_A)
    broken["head"] = {"ref": "a/b"}
    with pytest.raises(ValidationError):
        _run(
            _request(limit=10),
            [
                JsonExchange(
                    get(f"{_BASE}/pulls?state=open&per_page=10"),
                    json_response([broken]),
                )
            ],
        )
