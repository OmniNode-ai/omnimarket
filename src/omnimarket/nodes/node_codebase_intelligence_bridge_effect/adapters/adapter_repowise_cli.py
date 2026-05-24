# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repowise CLI adapter — satisfies ProtocolCodebaseIntelligence.

Invokes the `repowise` CLI as a subprocess and parses its JSON output.
Never imports .repowise/ internals.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# CLI subcommand mapping: operation → repowise subcommand
_OPERATION_SUBCOMMAND: dict[str, str] = {
    "get_answer": "get-answer",
    "get_context": "get-context",
    "get_symbol": "get-symbol",
    "search_codebase": "search",
    "get_why": "get-why",
}


class AdapterRepoWiseCLI:
    """Thin subprocess wrapper around the repowise CLI.

    Parameters
    ----------
    timeout_seconds:
        Per-query timeout passed via ``asyncio.wait_for``.
    cli_executable:
        Path/name of the repowise CLI binary. Defaults to ``repowise``.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        cli_executable: str = "repowise",
    ) -> None:
        self._timeout = timeout_seconds
        self._cli = cli_executable

    async def query(
        self,
        operation: str,
        query: str,
        targets: tuple[str, ...],
        include: tuple[str, ...],
    ) -> dict[str, Any]:
        """Run the repowise CLI and return the parsed JSON response.

        Raises
        ------
        asyncio.TimeoutError
            When the subprocess does not complete within ``timeout_seconds``.
        RuntimeError
            When the CLI exits non-zero or returns invalid JSON.
        """
        subcommand = _OPERATION_SUBCOMMAND.get(operation)
        if subcommand is None:
            raise ValueError(f"Unknown operation: {operation!r}")

        cmd = [self._cli, subcommand, "--json", query]
        if targets:
            for t in targets:
                cmd += ["--target", t]
        if include:
            for inc in include:
                cmd += ["--include", inc]

        logger.debug("repowise cmd: %s", cmd)

        async def _run() -> dict[str, Any]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                raise RuntimeError(f"repowise CLI exited {proc.returncode}: {err}")
            try:
                return json.loads(stdout.decode())  # type: ignore[no-any-return]
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"repowise CLI returned non-JSON output: {exc}"
                ) from exc

        return await asyncio.wait_for(_run(), timeout=self._timeout)


__all__ = ["AdapterRepoWiseCLI"]
