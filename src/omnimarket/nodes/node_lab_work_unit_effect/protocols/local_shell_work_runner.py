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

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Protocol

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.model_delegated_code_edit import (
    ModelCodeEditResult,
    ModelDelegatedCodeEditRequest,
)
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


def draft_correlation_id(work_unit_id: str) -> uuid.UUID:
    """The loop's correlation id for one unit: the same on every read of it."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"lab-work-unit:{work_unit_id}")


class LocalShellLabWorkExecutor:
    """Fetch, worktree, run, clean up; all local to this host."""

    def __init__(
        self,
        work_root: Path | None = None,
        url_base: str = service_endpoint_url(
            "github.web_url"
        ),  # tests pass a file:// base
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

    def _staged_diff(
        self, tree: Path, commit_sha: str, cap: int, env: dict[str, str]
    ) -> tuple[str, bool]:
        """The worktree's edits against ``commit_sha``, cut to ``cap`` bytes."""
        if not tree.is_dir():
            return "", False
        self._git("-C", str(tree), "add", "-A", env=env)
        diff = subprocess.run(
            ["git", "-C", str(tree), "diff", "--cached", "--no-color", commit_sha],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        ).stdout
        if len(diff) <= cap:
            return diff.decode("utf-8", errors="replace"), False
        return diff[:cap].decode("utf-8", errors="replace"), True

    @staticmethod
    def _loop_result(state_dir: Path, work_unit_id: str) -> ModelCodeEditResult | None:
        """The loop receipt's ``result``, when the loop wrote one."""
        path = (
            state_dir
            / "runs"
            / str(draft_correlation_id(work_unit_id))
            / "loop_receipt.json"
        )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return ModelCodeEditResult.model_validate(raw["result"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _draft_argv(
        self, request: ModelLabWorkUnitRequest, tree: Path, state_dir: Path
    ) -> list[str]:
        """The caller's argv plus the loop request file, an outside state root
        and the pinned backend, for a ``code_draft`` unit."""
        draft = request.code_draft
        if draft is None:
            return list(request.argv)
        loop_request = ModelDelegatedCodeEditRequest(
            correlation_id=str(draft_correlation_id(request.work_unit_id)),
            task=draft.task,
            workspace_root=str(tree),
            writable_globs=draft.writable_globs,
            context_paths=draft.context_paths,
            file_list=draft.file_list,
            checks=draft.checks,
            formatter=draft.formatter,
            max_turns=draft.max_turns,
            caller_lane=draft.caller_lane,
            ticket=draft.ticket,
            traffic_class=draft.traffic_class,
            source_surface=draft.source_surface,
        )
        request_file = self._root / "requests" / f"{request.work_unit_id}.json"
        request_file.parent.mkdir(parents=True, exist_ok=True)
        request_file.write_text(
            loop_request.model_dump_json(exclude_none=True), encoding="utf-8"
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        return [
            *request.argv,
            "--request",
            str(request_file),
            "--state-root",
            str(state_dir),
            "--backend-id",
            draft.backend_id,
        ]

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        env = _env()
        name = request.repo.rsplit("/", 1)[1]
        bare = self._root / "repos" / f"{name}.git"
        tree = self._root / "runs" / request.work_unit_id
        log = self._root / "logs" / f"{request.work_unit_id}.log"
        state_dir = self._root / "state" / request.work_unit_id
        log.parent.mkdir(parents=True, exist_ok=True)
        tree.parent.mkdir(parents=True, exist_ok=True)

        def receipt(
            status: EnumLabWorkUnitStatus,
            *,
            exit_code: int | None = None,
            duration: float = 0.0,
            detail: str = "",
        ) -> ModelLabWorkUnitReceipt:
            diff, truncated, loop_result = "", False, None
            if request.code_draft is not None:
                # Taken now, while the worktree exists: the finally below only
                # removes it, and a killed loop's edits are still in it.
                diff, truncated = self._staged_diff(
                    tree, request.commit_sha, request.code_draft.max_diff_bytes, env
                )
                loop_result = self._loop_result(state_dir, request.work_unit_id)
            return ModelLabWorkUnitReceipt(
                diff=diff,
                diff_truncated=truncated,
                code_edit_result=loop_result,
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
            argv = self._draft_argv(request, tree, state_dir)
            started = time.monotonic()
            with log.open("wb") as out:
                process = subprocess.Popen(
                    [exe, *argv[1:]],
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
