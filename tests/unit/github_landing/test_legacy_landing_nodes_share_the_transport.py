# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three older landing surfaces send through the shared transport (OMN-19831).

node_ci_rerun_effect (re-run and the empty-commit re-trigger),
node_merge_sweep_auto_merge_arm_effect (the legacy arm) and
node_pr_lifecycle_fix_effect's auto-rebase build their requests with
``omnimarket.github_landing.github_landing_requests`` and send them with
``UrllibGithubLandingTransport.send_sync``. Each test replaces ``send_sync``
and asserts the exact requests and the unchanged (bool, error) contract.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.github_landing.github_landing_requests import (
    ENABLE_AUTO_MERGE_MUTATION,
    create_commit_request,
    enable_auto_merge_request,
    fast_forward_ref_request,
    git_commit_request,
    git_ref_request,
    pull_request_request,
    rerun_failed_jobs_request,
    update_branch_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun import (
    HandlerCiRerunEffect,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import handler_auto_rebase

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
_REPO = "OmniNode-ai/omnimarket"
_HEAD = "c5dea513fd94a0e940938f13a798c685ab7a88cb"
_TOKEN = "test-token-value-not-real"  # local-path-ok: fake credential


def _ok(status: int, body: dict[str, object] | None) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=status, headers={}, body=body)


class _Script:
    def __init__(self, responses: list[ModelGithubHttpResponse | Exception]) -> None:
        self._responses: Iterator[ModelGithubHttpResponse | Exception] = iter(responses)
        self.sent: list[ModelGithubHttpRequest] = []

    def __call__(
        self, _self: UrllibGithubLandingTransport, request: ModelGithubHttpRequest
    ) -> ModelGithubHttpResponse:
        self.sent.append(request)
        answer = next(self._responses)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> None:
    def send_sync(
        self: UrllibGithubLandingTransport, request: ModelGithubHttpRequest
    ) -> ModelGithubHttpResponse:
        return script(self, request)

    monkeypatch.setattr(UrllibGithubLandingTransport, "send_sync", send_sync)


# --- node_ci_rerun_effect ----------------------------------------------------------


def test_rerun_sends_the_shared_rerun_request(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _Script([_ok(201, None)])
    _install(monkeypatch, script)
    assert HandlerCiRerunEffect()._rerun_sync("36278000979", _REPO, _TOKEN) == (
        True,
        None,
    )
    assert script.sent == [rerun_failed_jobs_request(_REPO, 36278000979)]


def test_rerun_reports_githubs_message(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _Script([_ok(403, {"message": "Resource not accessible by integration"})])
    _install(monkeypatch, script)
    assert HandlerCiRerunEffect()._rerun_sync("1", _REPO, _TOKEN) == (
        False,
        "Resource not accessible by integration",
    )


def test_rerun_reports_no_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Script([GithubLandingTransportError("POST x: no response")]))
    triggered, error = HandlerCiRerunEffect()._rerun_sync("1", _REPO, _TOKEN)
    assert triggered is False
    assert error is not None
    assert "no response" in error


@pytest.mark.parametrize(("repo", "run"), [("not-a-slug", "1"), (_REPO, "abc")])
def test_rerun_refuses_bad_input_without_a_call(
    monkeypatch: pytest.MonkeyPatch, repo: str, run: str
) -> None:
    script = _Script([])
    _install(monkeypatch, script)
    triggered, error = HandlerCiRerunEffect()._rerun_sync(run, repo, _TOKEN)
    assert triggered is False
    assert error
    assert script.sent == []


def test_empty_commit_sends_the_four_shared_git_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _ok(200, {"object": {"sha": _HEAD}}),
            _ok(200, {"tree": {"sha": "t" * 40}}),
            _ok(201, {"sha": "n" * 40}),
            _ok(200, {"ref": "refs/heads/feature"}),
        ]
    )
    _install(monkeypatch, script)
    result = HandlerCiRerunEffect()._empty_commit_sync(
        _REPO, "feature", _HEAD, 2962, _TOKEN
    )
    assert result == (True, None)
    assert script.sent == [
        git_ref_request(_REPO, "feature"),
        git_commit_request(_REPO, _HEAD),
        create_commit_request(
            _REPO,
            message="ci: re-trigger dropped required workflows on #2962 (OMN-13416)",
            tree_sha="t" * 40,
            parent_sha=_HEAD,
        ),
        fast_forward_ref_request(_REPO, "feature", "n" * 40),
    ]


def test_empty_commit_stops_when_the_head_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script([_ok(200, {"object": {"sha": "0" * 40}})])
    _install(monkeypatch, script)
    triggered, error = HandlerCiRerunEffect()._empty_commit_sync(
        _REPO, "feature", _HEAD, 2962, _TOKEN
    )
    assert triggered is False
    assert error is not None
    assert "head branch moved" in error
    assert len(script.sent) == 1


def test_empty_commit_reports_a_refused_git_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script([_ok(404, {"message": "Not Found"})])
    _install(monkeypatch, script)
    assert HandlerCiRerunEffect()._empty_commit_sync(
        _REPO, "feature", _HEAD, 2962, _TOKEN
    ) == (False, "Not Found")


# --- node_merge_sweep_auto_merge_arm_effect ------------------------------------------


def test_legacy_arm_sends_the_unchanged_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script([_ok(200, {"data": {"enablePullRequestAutoMerge": {}}})])
    _install(monkeypatch, script)
    assert HandlerAutoMergeArmEffect()._arm_sync("PR_x", _REPO, _TOKEN) == (True, None)
    (sent,) = script.sent
    assert sent == enable_auto_merge_request("PR_x", "SQUASH", expected_head_sha=None)
    assert sent.body is not None
    assert sent.body["query"] == ENABLE_AUTO_MERGE_MUTATION
    assert sent.body["variables"] == {"id": "PR_x", "method": "SQUASH"}


def test_legacy_arm_reports_graphql_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = [{"message": "Pull request Auto merge is not allowed for this repository"}]
    _install(monkeypatch, _Script([_ok(200, {"data": None, "errors": errors})]))
    armed, error = HandlerAutoMergeArmEffect()._arm_sync("PR_x", _REPO, _TOKEN)
    assert armed is False
    assert error is not None
    assert "not allowed" in error


# --- node_pr_lifecycle_fix_effect auto-rebase ------------------------------------------


def test_auto_rebase_reads_updates_and_rereads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_auto_rebase, "_resolve_github_token", lambda: SecretStr(_TOKEN)
    )
    new_head = "d" * 40
    script = _Script(
        [
            _ok(200, {"head": {"sha": _HEAD}}),
            _ok(202, {"message": "Updating pull request branch."}),
            _ok(200, {"head": {"sha": new_head}}),
        ]
    )
    _install(monkeypatch, script)
    adapter = handler_auto_rebase._LiveRebaseAdapter()
    assert adapter._update_branch_sync(_REPO, 2962) == new_head
    assert script.sent == [
        pull_request_request(_REPO, 2962),
        update_branch_request(_REPO, 2962, _HEAD),
        pull_request_request(_REPO, 2962),
    ]


def test_auto_rebase_raises_githubs_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_auto_rebase, "_resolve_github_token", lambda: SecretStr(_TOKEN)
    )
    script = _Script(
        [
            _ok(200, {"head": {"sha": _HEAD}}),
            _ok(422, {"message": "merge conflict between base and head"}),
        ]
    )
    _install(monkeypatch, script)
    with pytest.raises(RuntimeError, match="merge conflict"):
        handler_auto_rebase._LiveRebaseAdapter()._update_branch_sync(_REPO, 2962)


# --- the move is complete ------------------------------------------------------------

_LEGACY_MODULES = (
    _SRC / "nodes" / "node_ci_rerun_effect" / "handlers" / "handler_ci_rerun.py",
    _SRC
    / "nodes"
    / "node_merge_sweep_auto_merge_arm_effect"
    / "handlers"
    / "handler_auto_merge_arm.py",
    _SRC
    / "nodes"
    / "node_pr_lifecycle_fix_effect"
    / "handlers"
    / "handler_auto_rebase.py",
)
_OWN_HTTP = re.compile(
    r"\burllib\b|GITHUB_REST_URL|GITHUB_GRAPHQL_URL|\brest_json\b|\bgraphql_json\b|enablePullRequestAutoMerge"
)


def test_the_legacy_modules_no_longer_carry_their_own_http() -> None:
    for path in _LEGACY_MODULES:
        code = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        hits = [line for line in code if _OWN_HTTP.search(line)]
        assert hits == [], f"{path.name} still sends its own HTTP: {hits}"


def test_the_http_scan_positive_control() -> None:
    for line in (
        "import urllib.request",
        "from omnimarket.config.service_endpoints import GITHUB_REST_URL",
        'rest_json("GET", path, token=token)',
        '"  enablePullRequestAutoMerge(input: {pullRequestId: $id}) {"',
    ):
        assert _OWN_HTTP.search(line), line
