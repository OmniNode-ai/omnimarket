# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real attempt runner: one process, no shell, only the environment variables the attempt names."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping

from ..models.model_read_only_principal import ModelProbeAttempt
from ..protocols.protocol_attempt_runner import ModelAttemptRun

_TIMED_OUT = 124
_NOT_STARTED = 127


class SubprocessAttemptRunner:
    """Run an attempt's argv directly. ``PATH`` is passed so a program name resolves; nothing else is."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def run(self, attempt: ModelProbeAttempt) -> ModelAttemptRun:
        env = {
            name: self._environ[name]
            for name in ("PATH", *attempt.pass_env)
            if name in self._environ
        }
        try:
            done = subprocess.run(
                list(attempt.argv),
                env=env,
                capture_output=True,
                text=True,
                timeout=attempt.timeout_s,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return ModelAttemptRun(exit_code=_TIMED_OUT, text="")
        except OSError:
            return ModelAttemptRun(exit_code=_NOT_STARTED, text="")
        return ModelAttemptRun(
            exit_code=done.returncode, text=f"{done.stdout}\n{done.stderr}"
        )
