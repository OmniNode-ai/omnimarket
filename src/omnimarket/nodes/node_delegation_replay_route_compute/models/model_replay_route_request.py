# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReplayRouteRequest(BaseModel):
    """One earlier delegation prompt that a replay is about to send to a model.

    ``source_repo`` and ``source_sha`` are what the caller recovered about the
    run that first answered the prompt; ``check_exit_code`` is the exit code of
    the task's own check once it has run in the worktree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pid: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    src_backend_id: str | None = None
    source_repo: str | None = None
    source_sha: str | None = None
    check_commands: tuple[str, ...] = ()
    check_exit_code: int | None = None
    chat_gate_passed: bool | None = None


__all__: list[str] = ["ModelReplayRouteRequest"]
