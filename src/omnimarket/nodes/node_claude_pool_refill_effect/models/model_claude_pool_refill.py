# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the Claude pool refill is asked to do, and what it answers. No field carries a token value."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelClaudePoolRefillRequest(BaseModel):
    """One refill run.

    ``config_path``: the pool overlay; ``None`` reads
    ``$ONEX_WORKSPACE_CONFIG_ROOT/config/lab/claude_code_pool.yaml``. ``hosts``: overlay host names to
    serve, empty for every declared host. ``dry_run``: read and probe only, add and mark nothing.
    ``check_config``: load and validate the overlay, then stop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_path: str | None = None
    hosts: tuple[str, ...] = ()
    dry_run: bool = False
    check_config: bool = False


class ModelClaudePoolRefillResult(BaseModel):
    """The summary lines (labels, counts and verdicts, never a value) and the run's exit code.

    ``exit_code``: 0 when every served host answers ok, 1 when one does not, 2 on a refusal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    lines: tuple[str, ...] = ()
    refusal: str | None = None


__all__ = ["ModelClaudePoolRefillRequest", "ModelClaudePoolRefillResult"]
