# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the whole code tree a lab host unit runs from, from one commit of a clone, with rollback.

The tree is built whole from one commit with ``git archive`` (every file, dotfiles included), checked equal to
that commit's tree blob for blob, synced and smoked, swapped in by one rename, checked again, and renamed back on
any failure. The revision installed is recorded, so the tree always equals a named commit. Every setting is an
overlay value; nothing is hard-coded.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeCheck,
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)
from omnimarket.nodes.node_unit_tree_install_effect.overlay import (
    ModelUnitTreeInstallOverlay,
    ModelUnitTreeRow,
)

IGNORE = ("__pycache__", ".in_use", "*.bak*", ".orphaned_at", "*.pyc", ".DS_Store")
ROLLBACK_TAG = ".rollback-"
FAILED_TAG = ".failed-"
DETAIL_MAX = 600

Runner = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[str]]


def _default_runner(
    argv: Sequence[str], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=127, stdout="", stderr="command not found"
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=124, stdout="", stderr="timed out"
        )


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def ignored(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(part, g) for part in rel.split("/") for g in IGNORE)


def tree_of(top: Path, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    """``{path: blob}`` of an installed tree; symlinks count as their target text; ``exclude`` top-level paths are skipped."""
    out: dict[str, str] = {}
    for here, dirs, files in os.walk(top):
        dirs[:] = [
            d for d in dirs if d != ".git" and not ignored(d) and d not in exclude
        ]
        for name in files:
            full = os.path.join(here, name)
            if ignored(name):
                continue
            rel = os.path.relpath(full, top).replace(os.sep, "/")
            if rel.split("/")[0] in exclude:
                continue
            data = (
                os.readlink(full).encode()
                if os.path.islink(full)
                else Path(full).read_bytes()
            )
            out[rel] = git_blob(data)
    return out


class HandlerUnitTreeInstall:
    """``handle(request) -> result``."""

    def __init__(
        self,
        overlay: ModelUnitTreeInstallOverlay,
        environ: Mapping[str, str],
        runner: Runner | None = None,
    ) -> None:
        self.overlay = overlay
        self.environ = environ
        self.runner = runner if runner is not None else _default_runner

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult:
        if request.operation == "verify":
            return self.verify(request)
        if request.operation == "dry-run":
            return self.install(request, dry_run=True)
        return self.install(request, dry_run=False)

    # ------------------------------------------------------------------ install

    def install(
        self, r: ModelUnitTreeInstallRequest, dry_run: bool
    ) -> ModelUnitTreeInstallResult:
        row = self._row(r)
        if row is None:
            return self._result(
                r,
                "refused",
                [ModelUnitTreeCheck(name="unit", ok=False, detail="unknown unit")],
            )
        if r.host != row.host:
            return self._result(
                r,
                "refused",
                [ModelUnitTreeCheck(name="host", ok=False, detail="host mismatch")],
            )
        root = self._root()
        if root is None:
            return self._result(
                r,
                "refused",
                [
                    ModelUnitTreeCheck(
                        name="root", ok=False, detail="root variable missing"
                    )
                ],
            )
        clone = root / row.clone_relative
        dest = root / row.install_relative
        stamp = self.stamp()
        staged = dest.parent / f".{dest.name}.staged-{stamp}"
        try:
            commit = self.resolve(clone, r.ref or row.ref, row.command_timeout_seconds)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shipped = self.build(clone, commit, staged, row.command_timeout_seconds)
        except (
            OSError,
            subprocess.SubprocessError,
            tarfile.TarError,
            ValueError,
        ) as exc:
            shutil.rmtree(staged, ignore_errors=True)
            return self._result(
                r,
                "refused",
                [
                    ModelUnitTreeCheck(
                        name="build", ok=False, detail=str(exc)[:DETAIL_MAX]
                    )
                ],
                detail="staged build failed; nothing installed",
            )
        before = [self.equal(staged, shipped, row.preserve_paths)]
        if not self._ok(before) or dry_run:
            shutil.rmtree(staged, ignore_errors=True)
            refused = not self._ok(before)
            return self._result(
                r,
                "refused" if refused else "dry-run",
                before,
                commit=commit,
                detail="staged tree failed its checks; nothing installed"
                if refused
                else "",
            )
        had = dest.exists() or dest.is_symlink()
        rollback = dest.parent / f"{dest.name}{ROLLBACK_TAG}{stamp}"
        if had:
            os.rename(dest, rollback)
        try:
            os.rename(staged, dest)
            self._preserve(rollback, dest, row.preserve_paths)
        except OSError as exc:
            if dest.exists() and had:
                self._preserve(dest, rollback, row.preserve_paths)
                shutil.rmtree(dest, ignore_errors=True)
            if had:
                os.rename(rollback, dest)
            shutil.rmtree(staged, ignore_errors=True)
            return self._result(
                r,
                "refused",
                [*before, ModelUnitTreeCheck(name="swap", ok=False, detail=str(exc))],
                commit=commit,
                detail="swap failed; previous tree put back",
            )
        after = self._after(dest, row, shipped)
        if self._ok(after):
            self._record(root, row, dest, commit, stamp, rollback.name if had else "")
            self.prune(dest, row.keep)
            return self._result(
                r,
                "pass",
                after,
                commit=commit,
                kept=rollback.name if had else "",
                detail=f"installed {commit}"
                + (f"; previous tree kept as {rollback.name}" if had else ""),
            )
        failed = dest.parent / f"{dest.name}{FAILED_TAG}{stamp}"
        os.rename(dest, failed)
        if had:
            self._preserve(failed, rollback, row.preserve_paths)
            os.rename(rollback, dest)
        return self._result(
            r,
            "rolled-back" if had else "failed",
            after,
            commit=commit,
            restored=dest.name if had else "",
            detail=f"installed tree failed; kept as {failed.name}"
            + ("; previous tree back" if had else ""),
        )

    def verify(self, r: ModelUnitTreeInstallRequest) -> ModelUnitTreeInstallResult:
        row = self._row(r)
        if row is None:
            return self._result(
                r,
                "fail",
                [ModelUnitTreeCheck(name="unit", ok=False, detail="unknown unit")],
            )
        root = self._root()
        if root is None:
            return self._result(
                r,
                "fail",
                [
                    ModelUnitTreeCheck(
                        name="root", ok=False, detail="root variable missing"
                    )
                ],
            )
        record = root / row.record_relative
        if not record.is_file():
            return self._result(
                r,
                "fail",
                [
                    ModelUnitTreeCheck(
                        name="recorded-revision", ok=False, detail="no record"
                    )
                ],
            )
        commit = json.loads(record.read_text()).get("commit", "")
        dest = root / row.install_relative
        checks = [
            self.equal(
                dest,
                self.ship(
                    root / row.clone_relative, commit, row.command_timeout_seconds
                ),
                row.preserve_paths,
            )
        ]
        checks.extend(self.smoke(dest, row))
        return self._result(
            r, "pass" if self._ok(checks) else "fail", checks, commit=commit
        )

    # ------------------------------------------------------------------ build and compare

    def _row(self, r: ModelUnitTreeInstallRequest) -> ModelUnitTreeRow | None:
        for row in self.overlay.units:
            if row.name == r.unit:
                return row
        return None

    def _root(self) -> Path | None:
        value = self.environ.get(self.overlay.omni_root_env)
        if not value:
            return None
        return Path(value)

    @staticmethod
    def resolve(clone: Path, ref: str, timeout: int) -> str:
        if not ref:
            raise ValueError("no commit or branch given")
        for candidate in (ref, f"origin/{ref}"):
            done = subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "rev-parse",
                    "--verify",
                    "-q",
                    candidate + "^{commit}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip()
        raise ValueError(f"{ref} is no commit in {clone}")

    @staticmethod
    def build(clone: Path, commit: str, staged: Path, timeout: int) -> dict[str, str]:
        """Build the whole repository at ``commit`` into ``staged`` and return its ``{path: blob}``."""
        staged.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            ["git", "-C", str(clone), "archive", "--format=tar", commit],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
            tar.extractall(path=str(staged), filter="data")
        return HandlerUnitTreeInstall.ship(clone, commit, timeout)

    @staticmethod
    def ship(clone: Path, commit: str, timeout: int) -> dict[str, str]:
        """``{path: blob}`` that installing the whole tree at ``commit`` ships."""
        done = subprocess.run(
            ["git", "-C", str(clone), "ls-tree", "-r", "-z", commit],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        out: dict[str, str] = {}
        for entry in done.stdout.decode("utf-8", "replace").split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            _mode, otype, blob = meta.split()
            if otype != "blob":
                continue
            out[path] = blob
        return out

    def equal(
        self, top: Path, shipped: dict[str, str], exclude: tuple[str, ...]
    ) -> ModelUnitTreeCheck:
        try:
            actual = tree_of(top, exclude)
        except OSError as exc:
            return ModelUnitTreeCheck(
                name="tree-equal", ok=False, detail=str(exc)[:DETAIL_MAX]
            )
        if actual == shipped:
            return ModelUnitTreeCheck(name="tree-equal", ok=True)
        missing = sorted(set(shipped) - set(actual))
        extra = sorted(set(actual) - set(shipped))
        changed = sorted(
            p for p in set(actual) & set(shipped) if actual[p] != shipped[p]
        )
        detail = "; ".join(
            part
            for part in (
                f"missing {missing[:5]}" if missing else "",
                f"extra {extra[:5]}" if extra else "",
                f"changed {changed[:5]}" if changed else "",
            )
            if part
        )[:DETAIL_MAX]
        return ModelUnitTreeCheck(
            name="tree-equal", ok=False, detail=detail or "tree differs"
        )

    def smoke(self, dest: Path, row: ModelUnitTreeRow) -> list[ModelUnitTreeCheck]:
        checks: list[ModelUnitTreeCheck] = []
        for i, argv in enumerate(row.smoke_commands, 1):
            done = self.runner(list(argv), dest, row.command_timeout_seconds)
            ok = done.returncode == 0
            checks.append(
                ModelUnitTreeCheck(
                    name=f"smoke {i}",
                    ok=ok,
                    detail=(done.stderr or "").strip()[:DETAIL_MAX] if not ok else "",
                )
            )
        return checks

    def _after(
        self, dest: Path, row: ModelUnitTreeRow, shipped: dict[str, str]
    ) -> list[ModelUnitTreeCheck]:
        checks = [self.equal(dest, shipped, row.preserve_paths)]
        sync_argv = [a.replace("{install_path}", str(dest)) for a in row.sync_command]
        done = self.runner(sync_argv, dest, row.command_timeout_seconds)
        checks.append(
            ModelUnitTreeCheck(
                name="sync",
                ok=done.returncode == 0,
                detail=(done.stderr or "").strip()[:DETAIL_MAX]
                if done.returncode != 0
                else "",
            )
        )
        checks.extend(self.smoke(dest, row))
        return checks

    @staticmethod
    def _preserve(src: Path, dst: Path, paths: tuple[str, ...]) -> None:
        for path in paths:
            source = src / path
            if source.exists() or source.is_symlink():
                os.rename(source, dst / path)

    def _record(
        self,
        root: Path,
        row: ModelUnitTreeRow,
        dest: Path,
        commit: str,
        stamp: str,
        previous: str,
    ) -> None:
        record = root / row.record_relative
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "unit": row.name,
            "commit": commit,
            "at": stamp,
            "install_path": str(dest),
            "previous": previous,
        }
        self.write_atomic(record, json.dumps(payload, indent=2).encode())

    def prune(self, dest: Path, keep: int) -> None:
        parent = dest.parent
        names = [
            p.name
            for p in parent.iterdir()
            if p.name.startswith(dest.name + ROLLBACK_TAG)
            or p.name.startswith(dest.name + FAILED_TAG)
        ]
        names.sort()
        for name in names[keep:]:
            shutil.rmtree(parent / name, ignore_errors=True)

    @staticmethod
    def write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    @staticmethod
    def stamp() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    @staticmethod
    def _ok(checks: Sequence[ModelUnitTreeCheck]) -> bool:
        return all(check.ok for check in checks)

    def _result(
        self,
        r: ModelUnitTreeInstallRequest,
        verdict: str,
        checks: Sequence[ModelUnitTreeCheck],
        commit: str = "",
        kept: str = "",
        restored: str = "",
        detail: str = "",
    ) -> ModelUnitTreeInstallResult:
        return ModelUnitTreeInstallResult(
            operation=r.operation,
            unit=r.unit,
            verdict=verdict,
            commit=commit,
            checks=tuple(checks),
            kept=kept,
            restored=restored,
            detail=detail,
        )
