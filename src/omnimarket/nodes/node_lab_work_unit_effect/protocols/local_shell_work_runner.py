# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs one lab work unit on the host this process runs on (OMN-20105).

The sequence is the one ``onex-lab-run`` (OMN-20101) runs over ssh, now run by
the host itself: fetch the exact pushed sha into a bare repository under
``<work_root>/repos``, add a detached worktree of it, run the argv there with
its output written to ``<work_root>/logs/<work_unit_id>.log``, and remove the
worktree. The log stays, so the receipt's ``log_path`` can be read afterwards.

A private repository is fetched through ``gh auth git-credential``; a host with
no ``gh`` login can fetch public repositories only, which is why a caller
placing a private-repository unit asks for the ``gh`` tool.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    MAX_TAIL_CHARS,
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)

_EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_GIT_TIMEOUT_SECONDS = 600


class ProtocolLabWorkRunner(Protocol):
    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt: ...


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # A serve process started from a git hook would otherwise carry the hook's
    # repository location into every git call and every unit it runs.
    for name in (
        "PYTHONPATH",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    ):
        env.pop(name, None)
    extra = [str(Path(p).expanduser()) for p in _EXTRA_PATH]
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_TAIL_CHARS * 2))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text[-MAX_TAIL_CHARS:]


class LocalShellLabWorkExecutor:
    """Fetch, worktree, run, clean up; all local to this host."""

    def __init__(
        self,
        work_root: Path | None = None,
        url_base: str = "https://github.com",  # url-authority-ok: the git remote a pushed sha is fetched from; tests pass a file:// base
    ) -> None:
        self._root = work_root or Path.home() / "lab-run"
        self._url_base = url_base.rstrip("/")

    def _git(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        env = _env()
        name = request.repo.rsplit("/", 1)[1]
        bare = self._root / "repos" / f"{name}.git"
        tree = self._root / "runs" / request.work_unit_id
        log = self._root / "logs" / f"{request.work_unit_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        tree.parent.mkdir(parents=True, exist_ok=True)

        def receipt(
            status: EnumLabWorkUnitStatus,
            *,
            exit_code: int | None = None,
            duration: float = 0.0,
            detail: str = "",
        ) -> ModelLabWorkUnitReceipt:
            return ModelLabWorkUnitReceipt(
                work_unit_id=request.work_unit_id,
                target_host=request.target_host,
                host=host_name,
                repo=request.repo,
                commit_sha=request.commit_sha,
                lane=request.lane,
                kind=request.kind,
                status=status,
                exit_code=exit_code,
                log_path=str(log) if log.exists() else "",
                output_tail=_tail(log),
                duration_seconds=round(duration, 1),
                detail=detail[:2000],
            )

        try:
            if not bare.is_dir():
                made = self._git("init", "--bare", "-q", str(bare), env=env)
                if made.returncode != 0:
                    return receipt(
                        EnumLabWorkUnitStatus.INFRA_ERROR,
                        detail=f"git init --bare failed: {made.stderr.strip()}",
                    )
            has = self._git(
                "-C",
                str(bare),
                "cat-file",
                "-e",
                f"{request.commit_sha}^{{commit}}",
                env=env,
            )
            if has.returncode != 0:
                fetched = self._git(
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.helper=!gh auth git-credential",
                    "-C",
                    str(bare),
                    "fetch",
                    "--no-tags",
                    "-q",
                    f"{self._url_base}/{request.repo}.git",
                    request.commit_sha,
                    env=env,
                )
                if fetched.returncode != 0:
                    return receipt(
                        EnumLabWorkUnitStatus.INFRA_ERROR,
                        detail=(
                            f"fetch of {request.commit_sha[:12]} from {request.repo} failed "
                            f"on {host_name}: {fetched.stderr.strip()[:500]}"
                        ),
                    )
            added = self._git(
                "-C",
                str(bare),
                "worktree",
                "add",
                "--detach",
                "-q",
                str(tree),
                request.commit_sha,
                env=env,
            )
            if added.returncode != 0:
                return receipt(
                    EnumLabWorkUnitStatus.INFRA_ERROR,
                    detail=f"worktree add failed: {added.stderr.strip()[:500]}",
                )
            exe = shutil.which(request.argv[0], path=env["PATH"])
            if exe is None:
                return receipt(
                    EnumLabWorkUnitStatus.INFRA_ERROR,
                    detail=f"{request.argv[0]!r} is not on this host's PATH",
                )
            env.update(
                LAB_WORK_UNIT_ID=request.work_unit_id,
                LAB_WORK_SHA=request.commit_sha,
                LAB_WORK_REPO=request.repo,
                LAB_WORK_LANE=request.lane,
            )
            started = time.monotonic()
            with log.open("wb") as out:
                process = subprocess.Popen(
                    [exe, *request.argv[1:]],
                    cwd=tree,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                try:
                    code = process.wait(timeout=request.timeout_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    return receipt(
                        EnumLabWorkUnitStatus.TIMED_OUT,
                        duration=time.monotonic() - started,
                        detail=f"killed after {request.timeout_seconds}s",
                    )
            return receipt(
                EnumLabWorkUnitStatus.COMPLETED,
                exit_code=code,
                duration=time.monotonic() - started,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return receipt(
                EnumLabWorkUnitStatus.INFRA_ERROR, detail=f"{type(exc).__name__}: {exc}"
            )
        finally:
            if tree.exists():
                self._git(
                    "-C", str(bare), "worktree", "remove", "--force", str(tree), env=env
                )
                shutil.rmtree(tree, ignore_errors=True)
                self._git("-C", str(bare), "worktree", "prune", env=env)


__all__ = ["LocalShellLabWorkExecutor", "ProtocolLabWorkRunner"]
