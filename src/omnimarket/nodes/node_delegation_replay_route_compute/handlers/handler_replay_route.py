# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route one replay task to a chat call or to a worktree, and score a worktree task by its own check (OMN-26563).

The real-work replay sent repository-edit prompts, taken from harness runs that
read a repository, to chat endpoints with no repository and no tools, so no
model could pass them. A repository-edit task is one whose prompt says to edit
files in a worktree or to run a check, or whose first answer was a harness run.
Such a task is replayed only in a worktree of its repository at the source
run's sha and is accepted only when its own check exits 0; with no repository,
no recoverable source sha or no check it is a typed refusal that names the task.
A self-contained prompt is a chat call scored by the chat gate. The handler is
pure: it reads no file and calls no model.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_delegation_replay_route_compute.models.enum_replay_route import (
    EnumReplayAcceptBasis,
    EnumReplayRefusal,
    EnumReplayRoute,
)
from omnimarket.nodes.node_delegation_replay_route_compute.models.model_replay_route_answer import (
    ModelReplayRouteAnswer,
)
from omnimarket.nodes.node_delegation_replay_route_compute.models.model_replay_route_request import (
    ModelReplayRouteRequest,
)

_HARNESS_BACKEND_PREFIX = "harness:"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO_EDIT_PROMPT = re.compile(
    r"\bworktree\b"
    r"|\bcurrent directory\b"
    r"|\b(?:this|the) repo(?:sitory)?\b"
    r"|\bedit (?:exactly|only|one)\b"
    r"|\bverify(?: with| command)?\s*:"
    r"|\bverify with\b",
    re.IGNORECASE,
)
_VERIFY_LINE = re.compile(r"\bverify\b[^:\n]*:\s*(?P<rest>.+)$", re.IGNORECASE)
_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")


def _is_repo_edit(request: ModelReplayRouteRequest) -> bool:
    if (request.src_backend_id or "").startswith(_HARNESS_BACKEND_PREFIX):
        return True
    return _REPO_EDIT_PROMPT.search(request.prompt) is not None


def _check_commands(request: ModelReplayRouteRequest) -> tuple[str, ...]:
    if request.check_commands:
        return request.check_commands
    found: list[str] = []
    for line in request.prompt.splitlines():
        match = _VERIFY_LINE.search(line)
        if match is None:
            continue
        rest = match.group("rest")
        spans = _BACKTICK_SPAN.findall(rest)
        if spans:
            found.extend(span.strip() for span in spans)
        else:
            found.extend(part.strip().rstrip(".") for part in rest.split(";"))
    return tuple(command for command in found if command)


def _refuse(
    request: ModelReplayRouteRequest, refusal: EnumReplayRefusal, message: str
) -> ModelReplayRouteAnswer:
    return ModelReplayRouteAnswer(
        ok=False,
        pid=request.pid,
        repo_edit=True,
        refusal=refusal,
        message=message,
    )


class HandlerDelegationReplayRoute:
    """Decide how a replay task is sent and scored."""

    def handle(self, request: ModelReplayRouteRequest) -> ModelReplayRouteAnswer:
        if not _is_repo_edit(request):
            return ModelReplayRouteAnswer(
                ok=True,
                pid=request.pid,
                repo_edit=False,
                route=EnumReplayRoute.CHAT,
                accepted=request.chat_gate_passed,
                accept_basis=(
                    EnumReplayAcceptBasis.CHAT_GATE
                    if request.chat_gate_passed is not None
                    else None
                ),
            )
        repository = (request.source_repo or "").strip()
        if not repository:
            return _refuse(
                request,
                EnumReplayRefusal.REPOSITORY_MISSING,
                f"task {request.pid} is a repository-edit task with no repository: "
                "a bare chat replay is refused",
            )
        sha = (request.source_sha or "").strip()
        if not _FULL_SHA.match(sha):
            return _refuse(
                request,
                EnumReplayRefusal.SOURCE_SHA_UNRECOVERABLE,
                f"task {request.pid} names repository {repository} but its source "
                "run's sha is not a recovered 40-hex commit: it is not replayed bare",
            )
        commands = _check_commands(request)
        if not commands:
            return _refuse(
                request,
                EnumReplayRefusal.CHECK_MISSING,
                f"task {request.pid} in repository {repository} has no check command: "
                "a worktree task is scored by its own check only",
            )
        exit_code = request.check_exit_code
        return ModelReplayRouteAnswer(
            ok=True,
            pid=request.pid,
            repo_edit=True,
            route=EnumReplayRoute.WORKTREE,
            repository=repository,
            source_sha=sha,
            check_commands=commands,
            accepted=None if exit_code is None else exit_code == 0,
            accept_basis=(
                None if exit_code is None else EnumReplayAcceptBasis.CHECK_EXIT_CODE
            ),
        )
