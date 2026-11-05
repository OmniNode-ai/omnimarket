# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one command step as a plain process: no shell, no model provider, the idempotency key in the environment."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

ENV_RUN_KEY = "ONEX_RUN_KEY"
ENV_STEP_NAME = "ONEX_RUN_STEP"
ENV_STEP_KEY = "ONEX_RUN_STEP_KEY"
ENV_STEP_ATTEMPT = "ONEX_RUN_STEP_ATTEMPT"


class ModelStepCommandResult(BaseModel):
    """The exit code and the last line of standard output of one command step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    output: str
    error: str = ""


class ProtocolStepRunner(Protocol):
    """Runs a step's argument vector with its key; injected so a backend never hard-codes how a step runs."""

    async def run(
        self,
        argv: tuple[str, ...],
        run_key: str,
        step_name: str,
        attempt: int,
        heartbeat: Callable[[], None] | None = None,
        on_cancel: Callable[[], Awaitable[None]] | None = None,
    ) -> ModelStepCommandResult: ...


class SubprocessStepRunner:
    """The default step runner: ``asyncio`` subprocess, argument vector, key and attempt in the environment."""

    def __init__(self, heartbeat_interval_seconds: float | None = None) -> None:
        self._interval = heartbeat_interval_seconds

    async def run(
        self,
        argv: tuple[str, ...],
        run_key: str,
        step_name: str,
        attempt: int,
        heartbeat: Callable[[], None] | None = None,
        on_cancel: Callable[[], Awaitable[None]] | None = None,
    ) -> ModelStepCommandResult:
        env = {
            **os.environ,
            ENV_RUN_KEY: run_key,
            ENV_STEP_NAME: step_name,
            ENV_STEP_KEY: f"{run_key}/{step_name}",
            ENV_STEP_ATTEMPT: str(attempt),
        }
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communicate = asyncio.ensure_future(proc.communicate())
        try:
            if self._interval is None or heartbeat is None:
                await asyncio.wait({communicate})
            else:
                while not communicate.done():
                    heartbeat()
                    await asyncio.wait({communicate}, timeout=self._interval)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            communicate.cancel()
            await proc.wait()
            if on_cancel is not None:
                await on_cancel()
            raise
        out, err = communicate.result()
        lines = out.decode(errors="replace").strip().splitlines()
        return ModelStepCommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            output=lines[-1] if lines else "",
            error=err.decode(errors="replace").strip()[-400:],
        )
