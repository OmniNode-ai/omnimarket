# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19528 -- the merged_pr read names the merge commit when REST omits it.

On the deployed gateway ``GET /pulls/<n>`` answers a merged PR with no
``merge_commit_sha`` key, so every ``merged_pr`` read came back with
``merge_commit_sha=None`` and node_dod_verify refused each ticket's product
contract as unreadable. The GraphQL PR detail carries the merge commit
(``mergeCommitOid``); the read asks it only when REST left the sha out of a
merged PR.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelMergedPrResult,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO,
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
    pull_request,
)

pytestmark = pytest.mark.unit

_NUMBER = 4155
_MERGE_SHA = "3" * 40
_REQUEST = ModelGithubGatewayRequest.model_validate(
    {
        "operation": EnumGithubGatewayOperation.MERGED_PR,
        "repo": REPO,
        "pr_number": _NUMBER,
    }
)


def _rest(**fields: object) -> JsonExchange:
    body = {
        **pull_request(_NUMBER, body=""),
        "merged": True,
        "state": "closed",
        "merged_at": "2026-10-19T04:33:12Z",
    }
    # GitHub's answer on the deployed lane carries no merge_commit_sha key.
    body.pop("merge_commit_sha", None)
    body.update(fields)
    return JsonExchange(get(f"/repos/{REPO}/pulls/{_NUMBER}"), json_response(body))


def _read(
    rest: JsonExchange, *, detail: dict[str, object] | None = None
) -> tuple[ModelMergedPrResult, RecordedGatewayTransport]:
    transport = RecordedGatewayTransport([rest])
    if detail is not None:
        transport.pr_details[(REPO, _NUMBER)] = detail
    result = dispatch(_REQUEST, transport)
    assert isinstance(result, ModelMergedPrResult)
    return result, transport


def test_merged_pr_without_a_rest_merge_sha_takes_the_graphql_merge_commit() -> None:
    result, transport = _read(
        _rest(), detail={"merged": True, "mergeCommitOid": _MERGE_SHA}
    )

    assert result.merged is True
    assert result.merge_commit_sha == _MERGE_SHA
    assert transport.pr_detail_reads == [(REPO, _NUMBER)]


def test_merged_pr_with_a_rest_merge_sha_asks_no_graphql() -> None:
    # Positive control: the sha REST holds is the answer and costs no second read.
    result, transport = _read(_rest(merge_commit_sha="4" * 40))

    assert result.merge_commit_sha == "4" * 40
    assert transport.pr_detail_reads == []


def test_unmerged_pr_asks_no_graphql_and_has_no_merge_commit() -> None:
    result, transport = _read(_rest(merged=False, state="open", merged_at=None))

    assert (result.state, result.merged, result.merge_commit_sha) == (
        "OPEN",
        False,
        None,
    )
    assert transport.pr_detail_reads == []


def test_graphql_without_a_merge_commit_leaves_it_unset() -> None:
    result, _ = _read(_rest(), detail={"merged": True, "mergeCommitOid": None})

    assert result.merge_commit_sha is None


def test_a_graphql_failure_is_the_reads_failure() -> None:
    class Failing(RecordedGatewayTransport):
        def fetch_pr_detail(self, repo: str, pr_number: int) -> dict[str, object]:
            raise GitHubTransportError("GitHub request failed: HTTP 502")

    transport = Failing([_rest()])

    with pytest.raises(GitHubTransportError, match="HTTP 502"):
        dispatch(_REQUEST, transport)
