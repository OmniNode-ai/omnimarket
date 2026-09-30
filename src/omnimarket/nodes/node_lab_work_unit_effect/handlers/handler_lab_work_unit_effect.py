# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabWorkUnitEffect -- one heavy command at one pushed commit, on the
pool host this process runs on (OMN-20105).

Canonical def-B handler: ``handle(request: ModelLabWorkUnitRequest) ->
ModelLabWorkUnitReceipt``. No ``Plugin*`` base, no envelope type.

TRUST BOUNDARY. Anyone who can publish to the command topic can ask a pool
host to run a command, so before anything is fetched the handler refuses, with
a typed ``refused`` receipt:

* a unit addressed to another host (the serve process filters these out before
  the handler, so this is the second line, not the first);
* a repository owner outside ``allowed_owners``;
* an executable outside ``allowed_executables`` (the first argv word, by base
  name). The default set covers tests, builds, lint and lab-model code drafts
  (crush, claude). There is no Codex (operator ruling 2026-09-29). It holds
  no shell, but ``python`` and ``uv run`` execute whatever the repository at
  that sha holds, so the allowlist bounds the tool, not the code. The real
  boundary is who can publish to the command topic: the broker, and the owner
  check above (only code from the allowed GitHub owners is fetched).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import PurePosixPath
from typing import Literal

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_shell_work_runner import (
    LocalShellLabWorkExecutor,
    ProtocolLabWorkRunner,
)

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_OWNERS: frozenset[str] = frozenset({"OmniNode-ai"})
DEFAULT_ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "uv",
        "uvx",
        "pytest",
        "python",
        "python3",
        "ruff",
        "mypy",
        "pre-commit",
        "npm",
        "npx",
        "pnpm",
        "make",
        "cargo",
        "crush",
        "claude",
    }
)


class HandlerLabWorkUnitEffect:
    """EFFECT handler: run one lab work unit addressed to this host."""

    def __init__(
        self,
        host_name: str | None = None,
        runner: ProtocolLabWorkRunner | None = None,
        *,
        allowed_owners: frozenset[str] = DEFAULT_ALLOWED_OWNERS,
        allowed_executables: frozenset[str] = DEFAULT_ALLOWED_EXECUTABLES,
    ) -> None:
        # The pool name comes from the serve command line (a deployment fact);
        # a bare construction falls back to this machine's short host name.
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._runner = runner or LocalShellLabWorkExecutor()
        self._owners = allowed_owners
        self._executables = allowed_executables

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    @property
    def host_name(self) -> str:
        return self._host

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        refusal = self.refusal(request)
        if refusal is not None:
            return refusal
        return await asyncio.to_thread(self._runner.run, request, self._host)

    def refusal(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt | None:
        reason = ""
        owner = request.repo.split("/", 1)[0]
        exe = PurePosixPath(request.argv[0]).name
        if request.target_host != self._host:
            reason = (
                f"addressed to {request.target_host!r}, this host is {self._host!r}"
            )
        elif owner not in self._owners:
            reason = (
                f"repository owner {owner!r} is not one this host runs "
                f"(allowed: {sorted(self._owners)})"
            )
        elif exe not in self._executables:
            reason = (
                f"executable {exe!r} is not one this host runs "
                f"(allowed: {sorted(self._executables)})"
            )
        if not reason:
            return None
        logger.warning("lab work unit %s refused: %s", request.work_unit_id, reason)
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=self._host,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.REFUSED,
            detail=f"{reason}; nothing was fetched or run",
        )


__all__ = [
    "DEFAULT_ALLOWED_EXECUTABLES",
    "DEFAULT_ALLOWED_OWNERS",
    "HandlerLabWorkUnitEffect",
]
