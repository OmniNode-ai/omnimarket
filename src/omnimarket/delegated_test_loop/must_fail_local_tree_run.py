# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one test file against the code as it was before a change (OMN-20032).

The runner the verifier uses on the machine it already runs the head check on.
It takes a shared clone of the product repository, checks out the pre-change
commit, lays the PR's own test-side files over it from the change commit, and
runs the one test file with the interpreter of the check's own lock-exact
environment.

Two facts decide whether the result means anything, and both are checked rather
than assumed:

* The interpreter's environment has the product installed editable, pointing at
  the head clone. Left alone, the test would import the head's code and pass
  for the wrong reason. The clone's ``src`` is put first on ``PYTHONPATH`` and
  the import root of the first package is read back; when it is not under the
  throwaway tree, no test runs and the result says why. A run that never
  happened is ``infra_error`` downstream, never a pass.
* A shared clone keeps a real ``.git``, so a test that reads git state behaves
  the same here as at the head.

The tree is removed in a ``finally``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.delegated_test_loop.must_fail_control import (
    ModelMustFailRunRequest,
    ModelMustFailRunResult,
    PythonProvider,
)

_GIT_TIMEOUT_S = 300
_PROBE_TIMEOUT_S = 60
_DETAIL_CHARS = 380


def _git(
    args: list[str], *, cwd: Path, timeout: int = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _has_commit(repo_dir: Path, sha: str) -> bool:
    return _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir).returncode == 0


def _share_source_objects(repo_dir: Path, tree: Path) -> None:
    """Point the tree at the source's objects, even when the source is shallow.

    ``git clone --shared`` of a shallow repository falls back to a plain clone
    with no alternates, so the commit just fetched into the source is unreadable
    from the tree. ``--reference`` is refused for a shallow repository, so the
    alternates line is written directly. When the source's objects cannot be
    resolved, nothing is written and the checkout reports the failure.
    """
    objects = _git(
        ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
        cwd=repo_dir,
    )
    if objects.returncode != 0 or not objects.stdout.strip():
        return
    alternates = tree / ".git" / "objects" / "info" / "alternates"
    existing = alternates.read_text().splitlines() if alternates.exists() else []
    source = objects.stdout.strip()
    if source not in existing:
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text("".join(f"{line}\n" for line in [*existing, source]))


def _fail(detail: str) -> ModelMustFailRunResult:
    return ModelMustFailRunResult(detail=detail[:_DETAIL_CHARS])


class HandlerMustFailLocalTreeRun:
    """Runs a test file at the pre-change commit in a throwaway shared clone."""

    def __init__(self, python_for: PythonProvider) -> None:
        self._python_for = python_for

    def handle(self, request: ModelMustFailRunRequest) -> ModelMustFailRunResult:
        repo_dir = request.repo_dir
        for sha in (request.pre_change_sha, request.change_sha):
            if not _has_commit(repo_dir, sha):
                _git(["fetch", "--no-tags", "origin", sha], cwd=repo_dir)
                if not _has_commit(repo_dir, sha):
                    return _fail(f"commit {sha[:12]} is not in {repo_dir.name}")

        python, python_err = self._python_for(repo_dir)
        if python is None:
            return _fail(python_err or "no interpreter for the product environment")

        work = Path(tempfile.mkdtemp(prefix="must-fail-control-"))
        try:
            return self._run_in(work, request, python)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _fail(f"{type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run_in(
        self, work: Path, request: ModelMustFailRunRequest, python: Path
    ) -> ModelMustFailRunResult:
        tree = work / "tree"
        clone = _git(
            [
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(request.repo_dir),
                str(tree),
            ],
            cwd=work,
        )
        if clone.returncode != 0:
            return _fail(f"git clone failed: {clone.stderr.strip()}")
        _share_source_objects(request.repo_dir, tree)
        checkout = _git(
            ["checkout", "--quiet", "--detach", request.pre_change_sha], cwd=tree
        )
        if checkout.returncode != 0:
            return _fail(f"checkout failed: {checkout.stderr.strip()}")

        for rel in request.overlay_paths:
            blob = subprocess.run(
                ["git", "cat-file", "blob", f"{request.change_sha}:{rel}"],
                cwd=str(request.repo_dir),
                env=scrub_git_location_env(os.environ),
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
            if blob.returncode != 0:
                return _fail(f"cannot read {rel} at the change commit")
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)

        env = dict(scrub_git_location_env(os.environ))
        for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH"):
            env.pop(name, None)
        src = tree / "src"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(src), str(tree)] if src.is_dir() else [str(tree)]
        )

        shadow = self._shadowed_by(python, tree, env)
        if shadow is not None:
            return _fail(shadow)

        junit = work / "junit.xml"
        proc = subprocess.run(
            [
                str(python),
                "-m",
                "pytest",
                request.test_path,
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={junit}",
            ],
            cwd=str(tree),
            env=env,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
            check=False,
        )
        xml = junit.read_text(encoding="utf-8") if junit.is_file() else ""
        return ModelMustFailRunResult(
            junit_xml=xml,
            exit_code=proc.returncode,
            detail=(proc.stdout or proc.stderr or "")[-_DETAIL_CHARS:],
        )

    @staticmethod
    def _shadowed_by(python: Path, tree: Path, env: dict[str, str]) -> str | None:
        """None when the first package under ``src`` imports from the tree."""
        src = tree / "src"
        if not src.is_dir():
            return None
        packages = sorted(
            p.name for p in src.iterdir() if (p / "__init__.py").is_file()
        )
        if not packages:
            return None
        probe = subprocess.run(
            [
                str(python),
                "-c",
                "import importlib,sys;print(importlib.import_module(sys.argv[1]).__file__)",
                packages[0],
            ],
            cwd=str(tree),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
        located = probe.stdout.strip()
        if probe.returncode != 0 or not located:
            return (
                f"cannot import {packages[0]} from the earlier tree: "
                + (probe.stderr.strip()[-200:])
            )
        if not Path(located).resolve().is_relative_to(tree.resolve()):
            return (
                f"{packages[0]} imports from {located}, not from the earlier "
                "tree: the run would test the head's code"
            )
        return None


__all__ = ["HandlerMustFailLocalTreeRun"]
