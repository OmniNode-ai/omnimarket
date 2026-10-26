# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator fixture execution within the sanctioned delegation effect.

The offline caller supplies its installed adapter and fixture environment.
This handler has no customer inference bus route.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


class HandlerHarnessEvalProcess:
    """Run a bounded harness process and retain its diagnostic log."""

    def handle(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        timeout: int,
        log: Path,
        prompt_file: Path | None = None,
    ) -> int:
        with (
            log.open("w") as output,
            (prompt_file or Path(os.devnull)).open() as prompt,
        ):
            process = subprocess.Popen(
                command,
                env=env,
                stdin=prompt,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return 124
