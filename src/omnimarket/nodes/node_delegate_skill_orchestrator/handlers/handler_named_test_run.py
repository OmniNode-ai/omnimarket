# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a delegated code answer's named tests in a sandbox (OMN-17427).

The sandbox is a temporary workspace copy with a scrubbed environment, POSIX
resource limits, and a per-test timeout that kills the test's process group.
Resolved write paths must stay inside the copy, including through symlinks.
This is the runner LocalDelegationDispatchPort is given so a code-class answer
is graded on its own tests. Each named test runs in its own pytest process;
bad code and missing tests produce failed evidence rather than exceptions.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from functools import partial
from pathlib import Path

from omnimarket.delegation.graded_ladder.graders import extract_code_block
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
)


def _apply_resource_limits(timeout_seconds: int) -> None:
    """Bound CPU, address space, and file size in the POSIX child only."""
    import resource

    cpu_seconds = timeout_seconds + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    # macOS refuses an RLIMIT_AS below the current address space; the CPU and
    # file-size limits and the timeout still bound the child there.
    with suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))


def _write_file(sandbox: Path, relative: str, content: str) -> None:
    path = (sandbox / relative).resolve()
    if not path.is_relative_to(sandbox):
        raise ValueError(f"path {relative!r} resolves outside the sandbox")
    # Write along the resolved path one component at a time, never following
    # a symlink: one swapped in after the check fails the write (ELOOP or
    # ENOTDIR) instead of redirecting it outside the sandbox.
    *parents, name = path.relative_to(sandbox).parts
    nofollow = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory = os.open(sandbox, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    try:
        for part in parents:
            with suppress(FileExistsError):
                os.mkdir(part, dir_fd=directory)
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory
                )
            except NotADirectoryError as exc:
                raise FileExistsError(
                    exc.errno, f"{part!r} exists and is not a directory"
                ) from exc
            os.close(directory)
            directory = child
        handle = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow,
            0o600,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(content)


def _run_test(sandbox: Path, test_id: str, timeout_seconds: int) -> tuple[int, str]:
    env = {
        # The interpreter's own bin directory, not the parent's PATH: the
        # child environment carries nothing read from the caller's.
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin:/bin")),
        "HOME": str(sandbox),
        "PYTHONPATH": str(sandbox),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    with subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(sandbox),
            test_id,
        ],
        cwd=sandbox,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        preexec_fn=(
            partial(_apply_resource_limits, timeout_seconds)
            if os.name == "posix"
            else None
        ),
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                # Descendants can hold the captured pipes open after pytest exits.
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            stdout, stderr = proc.communicate()
            return 124, f"{stdout}\n{stderr}\ntimed out after {timeout_seconds}s"
        return proc.returncode, f"{stdout}\n{stderr}"


class HandlerNamedTestRun:
    """Prepare an isolated workspace and collect ordered named-test evidence."""

    def handle(self, request: ModelNamedTestRunRequest) -> ModelNamedTestRunResult:
        spec = request.spec
        if importlib.util.find_spec("pytest") is None:
            # Without pytest every test would exit non-zero, and a correct
            # answer would read as failing. Refused: no evidence, never a fail.
            return ModelNamedTestRunResult(
                status=EnumNamedTestRunStatus.REFUSED,
                detail=f"pytest is not installed for {sys.executable}",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="named-test-run-") as temporary:
                sandbox = Path(temporary).resolve()
                if spec.workspace_root is not None:
                    workspace = Path(spec.workspace_root)
                    if not workspace.is_dir():
                        reason = (
                            "is not a directory"
                            if workspace.exists()
                            else "does not exist"
                        )
                        return ModelNamedTestRunResult(
                            status=EnumNamedTestRunStatus.REFUSED,
                            detail=f"workspace_root {str(workspace)!r} {reason}"[:4000],
                        )
                    shutil.copytree(
                        workspace,
                        sandbox,
                        dirs_exist_ok=True,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", ".venv", "__pycache__", "node_modules"
                        ),
                    )
                for test_file in spec.test_files:
                    _write_file(sandbox, test_file.path, test_file.content)
                _write_file(
                    sandbox,
                    spec.answer_path,
                    extract_code_block(request.answer_content).rstrip() + "\n",
                )

                failed: list[str] = []
                first_exit_code = 0
                first_output = ""
                for test_id in spec.test_ids:
                    exit_code, output = _run_test(
                        sandbox, test_id, request.timeout_seconds
                    )
                    if exit_code != 0:
                        if not failed:
                            first_exit_code = exit_code
                            first_output = output[-1500:]
                        failed.append(test_id)

                total = len(spec.test_ids)
                detail = f"all {total} named test(s) passed"
                if failed:
                    summary = (
                        f"{len(failed)} of {total} named test(s) failed: "
                        + ", ".join(failed)
                    )
                    detail = f"{summary[:2499]}\n{first_output}"
                return ModelNamedTestRunResult(
                    status=EnumNamedTestRunStatus.COMPLETED,
                    run=ModelNamedTestRun(
                        test_ids=spec.test_ids,
                        failed_test_ids=tuple(failed),
                        exit_code=first_exit_code,
                    ),
                    detail=detail[:4000],
                )
        except (OSError, ValueError, RuntimeError) as exc:
            return ModelNamedTestRunResult(
                status=EnumNamedTestRunStatus.REFUSED,
                detail=f"cannot prepare or run named tests: {type(exc).__name__}: {exc}"[
                    :4000
                ],
            )
