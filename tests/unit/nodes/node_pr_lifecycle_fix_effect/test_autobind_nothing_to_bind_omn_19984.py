# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An autobind run with nothing left to bind reads DECLINED, not ERROR (OMN-19984).

When the companion a command would write has already merged, the rebuilt window
branch stages nothing, the push leaves it at OCC's default branch, and GitHub
answers ``POST /pulls`` with 422 "No commits between <base> and <branch>". The
handler used to see that as an exception and post a failing
``occ-autobind / outcome`` check-run on the product PR (omninode_infra#1823,
whose companion onex_change_control#13037 had merged).

These tests drive the real emitter and the real fix handler: the 422 must come
out as a neutral DECLINED check-run and a DECLINED typed outcome, and every
other fault must still come out as a failing ERROR one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from omnimarket.events.occ_companion import EnumOccBatchMode
from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOutcomeKind,
)
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome import (
    AUTOBIND_OUTCOME_CHECK_NAME,
    OUTCOME_MARKER_PREFIX,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    classify_companion_decline,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    NothingToBindError,
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
)

pytestmark = pytest.mark.unit

_EMITTER = (
    "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
)
_OUTCOME = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome"

_REPO = "OmniNode-ai/omninode_infra"
_PR = 1823
_HEAD = "c" * 40
_WINDOW = "auto/window-omninode-ai-omninode_infra-occ-autobind"
# The body GitHub returns with the 422, as rest_json surfaces it.
_NO_COMMITS_BODY = (
    '{"message":"Validation Failed","errors":[{"resource":"PullRequest",'
    f'"code":"custom","message":"No commits between dev and {_WINDOW}"}}],'
    '"documentation_url":"https://docs.github.com/rest/pulls/pulls#create-a-pull-request",'
    '"status":"422"}'
)
_OTHER_422_BODY = (
    '{"message":"Validation Failed","errors":[{"resource":"PullRequest",'
    '"code":"custom","message":"A pull request already exists for '
    f'OmniNode-ai:{_WINDOW}."}}],"status":"422"}}'
)

_COMMAND = ModelPrLifecycleFixCommand(
    correlation_id=UUID("0b3f7f0e-6d57-4d21-9c0e-2f1d6a9d4a11"),
    pr_number=_PR,
    repo=_REPO,
    block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
    ticket_id="OMN-19984",
    dry_run=False,
    requested_at=datetime.fromisoformat("2026-10-08T03:00:00+00:00"),
)


class _Verifier:
    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        return ModelOccCompanionVerification(verified=False, detail="no companion")


class _Github:
    """Records the check-run and comment writes the outcome reporter makes."""

    def __init__(self) -> None:
        self.check_runs: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if method == "GET" and path == f"/repos/{_REPO}/pulls/{_PR}":
            return {"head": {"sha": _HEAD}}
        if method == "GET" and path.startswith(f"/repos/{_REPO}/issues/{_PR}/comments"):
            return []
        if method == "POST" and path == f"/repos/{_REPO}/check-runs":
            assert body is not None
            self.check_runs.append(body)
            return {}
        if method == "POST" and path == f"/repos/{_REPO}/issues/{_PR}/comments":
            assert body is not None
            self.comments.append(body)
            return {}
        raise AssertionError(f"unexpected GitHub call: {method} {path}")


@contextmanager
def _github_surface(github: _Github) -> Iterator[None]:
    with patch(f"{_OUTCOME}.rest_json", github):
        yield


def _head(_repo: str, _pr: int, _token: str | None) -> str:
    return _HEAD


def _handler(emitter: OccCompanionEmitter) -> HandlerPrLifecycleFix:
    return HandlerPrLifecycleFix(
        occ_autobind_adapter=emitter,
        occ_companion_verifier=_Verifier(),
        outcome_token_resolver=lambda: "ghs_test_token",
        head_sha_resolver=_head,
    )


# ---------------------------------------------------------------------------
# The producer: only the 422 "No commits between" becomes NothingToBindError
# ---------------------------------------------------------------------------


def _open_window_pr(post_error: GitHubApiError) -> None:
    """Run ``_open_or_sync_occ_pr`` for a window whose PR create answers ``post_error``."""
    emitter = OccCompanionEmitter()

    def fake_rest(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "POST", (method, path)
        raise post_error

    with (
        patch(f"{_EMITTER}._resolve_github_token", return_value="token"),
        patch(f"{_EMITTER}.rest_json", side_effect=fake_rest),
        patch.object(emitter, "_first_open_pr_number", return_value=None),
        patch.object(emitter, "_occ_default_branch", return_value="dev"),
    ):
        emitter._open_or_sync_occ_pr(
            branch=_WINDOW, ticket="OMN-19984", repo=_REPO, pr_number=_PR
        )


def test_no_commits_between_422_on_pr_create_is_nothing_to_bind() -> None:
    with pytest.raises(NothingToBindError, match="No commits between dev and"):
        _open_window_pr(GitHubApiError(_NO_COMMITS_BODY, status_code=422))


@pytest.mark.parametrize(
    "error",
    [
        GitHubApiError(_OTHER_422_BODY, status_code=422),
        GitHubApiError(_NO_COMMITS_BODY, status_code=500),
        GitHubApiError(_NO_COMMITS_BODY),
        GitHubApiError("Bad credentials", status_code=401),
    ],
    ids=["other-422", "same-text-500", "same-text-no-status", "401"],
)
def test_every_other_pr_create_fault_is_still_a_github_error(
    error: GitHubApiError,
) -> None:
    with pytest.raises(GitHubApiError) as raised:
        _open_window_pr(error)
    assert raised.value is error


def test_emitter_turns_nothing_to_bind_into_a_skip_decline() -> None:
    emitter = OccCompanionEmitter()
    with patch.object(
        emitter,
        "_emit_companion_sync_once",
        side_effect=NothingToBindError(f"{_WINDOW} has no commit past dev"),
    ):
        action = emitter._emit_companion_sync(
            _REPO, _PR, "OMN-19984", batch_mode=EnumOccBatchMode.WINDOW
        )

    assert action.startswith("skip:NOTHING_TO_BIND — "), action
    assert f"{_REPO}#{_PR}" in action
    code, occ_pr, stamped = classify_companion_decline(action)
    assert code is EnumPrLandingCompanionDeclineCode.NOTHING_TO_BIND
    assert occ_pr is None
    assert stamped is None


# ---------------------------------------------------------------------------
# The reporter: DECLINED for nothing-to-bind, ERROR for everything else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_to_bind_posts_a_neutral_declined_outcome() -> None:
    emitter = OccCompanionEmitter()
    github = _Github()
    with (
        _github_surface(github),
        patch.object(
            emitter,
            "_emit_companion_sync_once",
            side_effect=NothingToBindError(f"{_WINDOW} has no commit past dev"),
        ),
    ):
        result, outcome = await _handler(emitter).handle_with_companion_outcome(
            _COMMAND
        )

    assert result.error is None
    assert len(github.check_runs) == 1
    check_run = github.check_runs[0]
    assert check_run["name"] == AUTOBIND_OUTCOME_CHECK_NAME
    assert check_run["head_sha"] == _HEAD
    assert check_run["conclusion"] == "neutral"
    summary = check_run["output"]["summary"]
    assert summary.startswith(f"{OUTCOME_MARKER_PREFIX} DECLINED ")
    assert "skip:NOTHING_TO_BIND" in summary
    assert github.comments == [], "a decline never comments on the product PR"

    assert outcome is not None
    assert outcome.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
    assert outcome.decline_code is EnumPrLandingCompanionDeclineCode.NOTHING_TO_BIND


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        GitHubApiError(_OTHER_422_BODY, status_code=422),
        GitHubApiError("Bad credentials", status_code=401),
        RuntimeError("Could not parse the provided public key."),
    ],
    ids=["other-422", "401", "runtime-error"],
)
async def test_every_real_error_still_posts_a_failing_error_outcome(
    error: Exception,
) -> None:
    emitter = OccCompanionEmitter()
    github = _Github()
    with (
        _github_surface(github),
        patch.object(emitter, "_emit_companion_sync_once", side_effect=error),
    ):
        result, outcome = await _handler(emitter).handle_with_companion_outcome(
            _COMMAND
        )

    assert result.error is not None
    assert len(github.check_runs) == 1
    check_run = github.check_runs[0]
    assert check_run["conclusion"] == "failure"
    assert check_run["output"]["summary"].startswith(f"{OUTCOME_MARKER_PREFIX} ERROR ")
    assert len(github.comments) == 1, "an ERROR also notifies the product PR"

    assert outcome is not None
    assert outcome.kind is EnumPrLandingCompanionOutcomeKind.ERROR
