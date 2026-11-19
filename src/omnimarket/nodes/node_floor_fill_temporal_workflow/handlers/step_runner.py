# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one step as a plain process: no shell, the step key in the environment, the step input on stdin.

The shape is workflow 1's step runner (prove-running), which follows the stage A pilot's (run engine node): an
argument vector, ``ONEX_RUN_STEP_KEY`` and ``ONEX_RUN_STEP_ATTEMPT`` in the environment, and the last line of standard output as the
step's answer. Temporal delivers an activity at least once, so the command owns making a repeat delivery a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict

ENV_STEP_KEY = "ONEX_RUN_STEP_KEY"
ENV_STEP_NAME = "ONEX_RUN_STEP"
ENV_STEP_ATTEMPT = "ONEX_RUN_STEP_ATTEMPT"


class ModelStepProcessResult(BaseModel):
    """The exit code, the last line of standard output and the tail of standard error of one step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    output: str
    error: str = ""


async def run_step_process(
    argv: tuple[str, ...],
    stdin: str,
    *,
    step_name: str,
    step_key: str,
    attempt: int,
    base_env: Mapping[str, str],
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval_seconds: float = 10.0,
) -> ModelStepProcessResult:
    """Run ``argv`` with ``stdin`` over ``base_env`` (the worker's environment, handed in by the worker); heartbeat
    while it runs; kill it when the activity is cancelled."""
    env = {
        **base_env,
        ENV_STEP_KEY: step_key,
        ENV_STEP_NAME: step_name,
        ENV_STEP_ATTEMPT: str(attempt),
    }
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate = asyncio.ensure_future(proc.communicate(stdin.encode()))
    try:
        while not communicate.done():
            if heartbeat is not None:
                heartbeat()
            await asyncio.wait({communicate}, timeout=heartbeat_interval_seconds)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        communicate.cancel()
        await proc.wait()
        raise
    out, err = communicate.result()
    lines = out.decode(errors="replace").strip().splitlines()
    return ModelStepProcessResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=lines[-1] if lines else "",
        error=err.decode(errors="replace").strip()[-400:],
    )
