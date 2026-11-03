# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Codex attempts launched by a host dispatcher, with the child sandbox on.

Run outside the caller agent's tool sandbox, just like remote-lane's engine.
Codex 0.160 requires a read/write installation_id in the existing CODEX_HOME
before it starts a turn, even with --ephemeral. A nested caller sandbox can
refuse that startup; this handler reports it and never changes host credentials,
CODEX_HOME, or the enclosing sandbox to evade that refusal.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnimarket.handlers.container import container_overlay_dependencies as container

from ..codex_harness_policy import PACKAGE, load_codex_harness_policy
from ..models.model_codex_harness_attempt import (
    ModelCodexHarnessPolicy,
    ModelCodexHarnessReceipt,
    ModelCodexHarnessRequest,
)


def _terminate(process: subprocess.Popen[str], grace: float) -> tuple[str, str]:
    """Reap only this attempt's process group, including inherited pipe holders."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            return process.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    # SIGKILL has been delivered to the entire group. Reap the direct child.
    return process.communicate()


def _counter(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _receipt(
    request: ModelCodexHarnessRequest,
    *,
    started: float,
    code: int,
    stdout: str,
    stderr: str,
    answer_file: Path,
    timed_out: bool,
) -> ModelCodexHarnessReceipt:
    usage: dict[str, Any] = {}
    completed = failed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            completed = True
            raw = event.get("usage")
            usage = raw if isinstance(raw, dict) else {}
        elif event.get("type") in {"turn.failed", "error"}:
            failed = True
    try:
        answer = (
            answer_file.read_text(encoding="utf-8") if answer_file.is_file() else ""
        )
    except (OSError, UnicodeError):
        answer = ""
    cause: Any = "completed"
    if timed_out:
        cause = "timeout"
    elif "failed to initialize in-process app-server client" in stderr:
        cause = "startup_refused"
    elif code != 0:
        cause = "process_failed"
    elif failed:
        cause = "turn_failed"
    elif not completed or not answer.strip():
        cause = "invalid_output"
    return ModelCodexHarnessReceipt(
        answer=answer if cause == "completed" else "",
        tokens_in=_counter(usage, "input_tokens"),
        tokens_out=_counter(usage, "output_tokens"),
        tokens_cached=_counter(usage, "cached_input_tokens"),
        wall_seconds=time.monotonic() - started,
        model=request.model,
        exit_cause=cause,
        exit_code=code,
    )


class HandlerCodexHarnessAttempt:
    """Execute one bounded local attempt through the existing delegation effect."""

    def __init__(
        self,
        policy: ModelCodexHarnessPolicy | None = None,
        dependencies: Mapping[str, object] | None = None,
    ) -> None:
        bound = (
            dependencies
            if dependencies is not None
            else container.require_dependencies(
                container.declared_handler_dependencies(PACKAGE)
            )
        )
        self._environment = container.process_environment(bound)
        self.policy = (
            policy
            if policy is not None
            else load_codex_harness_policy(container.overlay_documents(bound))
        )

    def handle(self, request: ModelCodexHarnessRequest) -> ModelCodexHarnessReceipt:
        if request.timeout_seconds > self.policy.max_timeout_seconds:
            raise ValueError("requested timeout exceeds contract policy")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(
            prefix="codex-attempt-", dir=self.policy.workdir
        ) as scratch:
            answer_file = Path(scratch) / "answer.txt"
            argv = [
                self.policy.executable,
                "exec",
                "-C",
                str(self.policy.workdir.resolve()),
                "-c",
                f"model_reasoning_effort={json.dumps(self.policy.reasoning_effort)}",
                "-c",
                f"approval_policy={json.dumps(self.policy.approval_policy)}",
                "-s",
                self.policy.sandbox,
                "-c",
                f"sandbox_workspace_write.network_access={str(self.policy.network_access).lower()}",
                "--skip-git-repo-check",
                "--json",
                "-m",
                request.model,
                "-o",
                str(answer_file),
                "-",
            ]
            # Preserve the host account/config and runtime environment as remote-lane
            # does. Do not force a login method, replace HOME or copy credentials.
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=self.policy.workdir,
                    env=self._environment.environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError:
                return ModelCodexHarnessReceipt(
                    wall_seconds=time.monotonic() - started,
                    model=request.model,
                    exit_cause="launch_failed",
                )
            timed_out = False
            try:
                stdout, stderr = process.communicate(
                    request.prompt, timeout=request.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                stdout, stderr = _terminate(
                    process, self.policy.terminate_grace_seconds
                )
            except BaseException:
                _terminate(process, self.policy.terminate_grace_seconds)
                raise
            assert process.returncode is not None
            return _receipt(
                request,
                started=started,
                code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                answer_file=answer_file,
                timed_out=timed_out,
            )
