# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26563: a repository-edit task never replays as a bare chat call.

The real-work replay sent ten repository-edit prompts, taken from harness runs
that read a repository, to chat endpoints with no repository and no tools, so no
model could pass them. The route decision below keeps a repository task out of
a chat call, refuses one with no repository or no recoverable source sha, scores
a worktree task by its own check, and leaves a self-contained prompt as chat.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_replay_route_compute.handlers.handler_replay_route import (
    HandlerDelegationReplayRoute,
)
from omnimarket.nodes.node_delegation_replay_route_compute.models.enum_replay_route import (
    EnumReplayAcceptBasis,
    EnumReplayRefusal,
    EnumReplayRoute,
)
from omnimarket.nodes.node_delegation_replay_route_compute.models.model_replay_route_request import (
    ModelReplayRouteRequest,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40
HARNESS_BACKEND = "harness:claude-glm:glm-5.3-flash"
REPO_PROMPT = (
    "Task: in this omnimarket worktree, make the capture silence watch's unit tick "
    "exit 0 when the node delivered an alarm.\n\n"
    "Files (only these):\n1. src/omnimarket/nodes/node_capture_silence_watch_effect/runtime_tick.py\n\n"
    "Verify: run `uv run pytest tests/nodes/node_capture_silence_watch_effect/test_schedule.py -q -n 0` "
    "and `uv run ruff check src/omnimarket/nodes/node_capture_silence_watch_effect`."
)
SELF_CONTAINED_PROMPT = (
    "Write a Python function slugify(s: str) -> str that lowercases s, replaces runs of "
    "non-alphanumeric characters with a single hyphen and strips leading and trailing hyphens. "
    "Return only the code."
)


def _request(**overrides: object) -> ModelReplayRouteRequest:
    fields: dict[str, object] = {
        "pid": "r4-code-1",
        "task_type": "code_generation",
        "prompt": REPO_PROMPT,
        "src_backend_id": HARNESS_BACKEND,
        "source_repo": "omnimarket",
        "source_sha": SHA,
        "check_commands": ("uv run pytest tests/test_a.py -q",),
    }
    fields.update(overrides)
    return ModelReplayRouteRequest.model_validate(fields)


def test_repo_task_is_sent_to_a_worktree_never_a_chat_call() -> None:
    answer = HandlerDelegationReplayRoute().handle(_request())
    assert answer.ok
    assert answer.repo_edit
    assert answer.route == EnumReplayRoute.WORKTREE
    assert answer.repository == "omnimarket"
    assert answer.source_sha == SHA


def test_repo_task_with_no_repository_is_refused_and_names_task_and_repository() -> (
    None
):
    answer = HandlerDelegationReplayRoute().handle(_request(source_repo=None))
    assert not answer.ok
    assert answer.route is None
    assert answer.refusal == EnumReplayRefusal.REPOSITORY_MISSING
    assert "r4-code-1" in answer.message
    assert "repository" in answer.message


def test_harness_sourced_task_with_no_repository_is_refused_even_without_edit_words() -> (
    None
):
    answer = HandlerDelegationReplayRoute().handle(
        _request(prompt=SELF_CONTAINED_PROMPT, source_repo=None, source_sha=None)
    )
    assert not answer.ok
    assert answer.refusal == EnumReplayRefusal.REPOSITORY_MISSING


@pytest.mark.parametrize("sha", [None, "", "HEAD", "abc123", "g" * 40])
def test_repo_task_whose_source_sha_is_unrecoverable_is_refused_not_replayed_bare(
    sha: str | None,
) -> None:
    answer = HandlerDelegationReplayRoute().handle(_request(source_sha=sha))
    assert not answer.ok
    assert answer.route is None
    assert answer.refusal == EnumReplayRefusal.SOURCE_SHA_UNRECOVERABLE
    assert "r4-code-1" in answer.message


def test_self_contained_prompt_still_replays_as_a_chat_call() -> None:
    answer = HandlerDelegationReplayRoute().handle(
        _request(
            pid="s0",
            prompt=SELF_CONTAINED_PROMPT,
            src_backend_id=None,
            source_repo=None,
            source_sha=None,
            check_commands=(),
            chat_gate_passed=True,
        )
    )
    assert answer.ok
    assert not answer.repo_edit
    assert answer.route == EnumReplayRoute.CHAT
    assert answer.accepted is True
    assert answer.accept_basis == EnumReplayAcceptBasis.CHAT_GATE


def test_worktree_task_is_scored_by_its_own_check_exit_code() -> None:
    handler = HandlerDelegationReplayRoute()
    passed = handler.handle(_request(check_exit_code=0, chat_gate_passed=False))
    failed = handler.handle(_request(check_exit_code=1, chat_gate_passed=True))
    unscored = handler.handle(_request(check_exit_code=None, chat_gate_passed=True))
    assert passed.accepted is True
    assert passed.accept_basis == EnumReplayAcceptBasis.CHECK_EXIT_CODE
    assert failed.accepted is False
    assert failed.accept_basis == EnumReplayAcceptBasis.CHECK_EXIT_CODE
    assert unscored.accepted is None


def test_check_is_taken_from_the_prompts_verify_line_when_not_supplied() -> None:
    answer = HandlerDelegationReplayRoute().handle(_request(check_commands=()))
    assert answer.ok
    assert answer.check_commands == (
        "uv run pytest tests/nodes/node_capture_silence_watch_effect/test_schedule.py -q -n 0",
        "uv run ruff check src/omnimarket/nodes/node_capture_silence_watch_effect",
    )


def test_repo_task_with_no_check_anywhere_is_refused() -> None:
    prompt = "Task: in this worktree, rename the helper foo to bar in src/a.py."
    answer = HandlerDelegationReplayRoute().handle(
        _request(prompt=prompt, check_commands=())
    )
    assert not answer.ok
    assert answer.refusal == EnumReplayRefusal.CHECK_MISSING
