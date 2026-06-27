# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WS-5 Wave 3 — multi-parameter integration coverage for node_auto_merge_effect.

EFFECT node (Variant A): the handler is driven in-process via its injected
``run_fn`` seam (the gh-CLI boundary). Each parametrized case feeds a distinct
sequence of synthetic ``gh`` responses and asserts the TYPED
``ModelAutoMergeResult`` fields (merged flag, merge_commit_sha, blocked_reason,
ticket_close_status) — never "handler returned without raising".

Negative controls: the DIRTY-merge-state case and the CHANGES_REQUESTED case
must each produce ``merged=False`` with a populated ``blocked_reason``; the
invalid-strategy case must be blocked before any merge call.

No subprocess / asyncpg is monkeypatched — the gh boundary is the constructor
``run_fn`` collaborator (the canonical ``_Mock*`` injection pattern).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import pytest

from omnimarket.nodes.node_auto_merge_effect.handlers.handler_auto_merge_effect import (
    HandlerAutoMergeEffect,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_input import (
    ModelAutoMergeInput,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_result import (
    ModelAutoMergeResult,
)

REPO = "OmniNode-ai/omnimarket"


def _pr_view(merge_state: str = "CLEAN", review_decision: str = "APPROVED") -> str:
    return json.dumps(
        {
            "mergeStateStatus": merge_state,
            "statusCheckRollup": [],
            "reviewDecision": review_decision,
            "latestReviews": [],
        }
    )


def _scripted_run(
    responses: list[tuple[int, str, str]],
) -> Callable[[list[str]], tuple[int, str, str]]:
    """Return a run_fn that replays a fixed sequence of (rc, stdout, stderr)."""
    state = {"idx": 0}

    def _run(_cmd: list[str]) -> tuple[int, str, str]:
        i = state["idx"]
        state["idx"] = i + 1
        return responses[i]

    return _run


# Clean merge: poll CLEAN -> CodeRabbit gate clean -> merge ok -> SHA -> branch.
_CLEAN_SEQUENCE = [
    (0, _pr_view("CLEAN", "APPROVED"), ""),
    (0, _pr_view("CLEAN", "APPROVED"), ""),
    (0, "", ""),
    (0, json.dumps({"mergeCommit": {"oid": "cafef00dbabe"}}), ""),
    (0, json.dumps({"headRefName": "jonah/omn-1-x"}), ""),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "responses", "expect"),
    [
        pytest.param(
            {},
            _CLEAN_SEQUENCE,
            {"merged": True, "sha": "cafef00dbabe", "blocked_substr": None},
            id="clean-squash-merged",
        ),
        pytest.param(
            {"strategy": "merge"},
            _CLEAN_SEQUENCE,
            {"merged": True, "sha": "cafef00dbabe", "blocked_substr": None},
            id="strategy-merge-merged",
        ),
        pytest.param(
            {"delete_branch": False},
            _CLEAN_SEQUENCE,
            {"merged": True, "sha": "cafef00dbabe", "blocked_substr": None},
            id="no-delete-branch-merged",
        ),
        # NEGATIVE CONTROL: dirty merge state must block with a conflict reason.
        pytest.param(
            {},
            [(0, _pr_view("DIRTY"), "")],
            {"merged": False, "sha": None, "blocked_substr": "merge conflicts"},
            id="dirty-blocked",
        ),
        # NEGATIVE CONTROL: CHANGES_REQUESTED review must block at the CR gate.
        pytest.param(
            {},
            [
                (0, _pr_view("CLEAN", "CHANGES_REQUESTED"), ""),
                (0, _pr_view("CLEAN", "CHANGES_REQUESTED"), ""),
            ],
            {
                "merged": False,
                "sha": None,
                "blocked_substr": "CHANGES_REQUESTED",
            },
            id="changes-requested-blocked",
        ),
        # NEGATIVE CONTROL: invalid strategy is rejected before any merge call.
        pytest.param(
            {"strategy": "bogus"},
            [
                (0, _pr_view("CLEAN", "APPROVED"), ""),
                (0, _pr_view("CLEAN", "APPROVED"), ""),
            ],
            {
                "merged": False,
                "sha": None,
                "blocked_substr": "Invalid merge strategy",
            },
            id="invalid-strategy-blocked",
        ),
        # Hard error from gh pr view (exit 1) must block, not raise.
        pytest.param(
            {},
            [(1, "", "gh: not found")],
            {"merged": False, "sha": None, "blocked_substr": "gh pr view failed"},
            id="gh-error-blocked",
        ),
    ],
)
async def test_auto_merge_multiparam(
    overrides: dict[str, object],
    responses: list[tuple[int, str, str]],
    expect: dict[str, object],
) -> None:
    correlation_id = uuid4()
    payload = ModelAutoMergeInput(
        correlation_id=correlation_id,
        pr_number=4242,
        repo=REPO,
        **overrides,
    )
    handler = HandlerAutoMergeEffect(run_fn=_scripted_run(responses))
    handler._sleep = lambda _s: None  # type: ignore[method-assign]

    result = await handler.handle(payload)

    assert isinstance(result, ModelAutoMergeResult)
    assert result.correlation_id == correlation_id
    assert result.pr_number == 4242
    assert result.merged is expect["merged"]
    if expect["sha"] is None:
        assert result.merge_commit_sha is None
    else:
        assert result.merge_commit_sha == expect["sha"]
    if expect["blocked_substr"] is None:
        assert result.blocked_reason is None
    else:
        assert result.blocked_reason is not None
        assert expect["blocked_substr"] in result.blocked_reason


@pytest.mark.integration
async def test_auto_merge_timeout_blocks_when_never_clean() -> None:
    """gate_timeout exhausted while state stays BLOCKED -> timeout block (not merge)."""
    payload = ModelAutoMergeInput(
        correlation_id=uuid4(),
        pr_number=7,
        repo=REPO,
        gate_timeout_hours=0.0001,  # ~0.36s wall-clock budget
    )

    def _always_blocked(_cmd: list[str]) -> tuple[int, str, str]:
        return (0, _pr_view("BLOCKED"), "")

    handler = HandlerAutoMergeEffect(run_fn=_always_blocked)
    handler._sleep = lambda _s: None  # type: ignore[method-assign]

    result = await handler.handle(payload)

    assert result.merged is False
    assert result.blocked_reason is not None
    assert "timed out" in result.blocked_reason


@pytest.mark.integration
async def test_auto_merge_closes_ticket_via_injected_collaborator() -> None:
    """ticket_id + injected close_ticket_fn -> ticket_close_status reflects the close."""
    closed: list[str] = []

    def _close(ticket: str) -> str:
        closed.append(ticket)
        return "closed"

    payload = ModelAutoMergeInput(
        correlation_id=uuid4(),
        pr_number=99,
        repo=REPO,
        ticket_id="OMN-13677",
    )
    handler = HandlerAutoMergeEffect(
        run_fn=_scripted_run(_CLEAN_SEQUENCE),
        close_ticket_fn=_close,
    )
    handler._sleep = lambda _s: None  # type: ignore[method-assign]

    result = await handler.handle(payload)

    assert result.merged is True
    assert result.ticket_close_status == "closed"
    assert closed == ["OMN-13677"]
