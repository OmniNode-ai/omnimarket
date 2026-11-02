# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install an overlay tree from one commit, checked before and after, with rollback.

The overlay plugin is data only: a ``.claude-plugin/plugin.json`` and ``overlays/<name>/overlay.yaml`` files. This
installs it the way a plugin tree is installed: the whole tree is built from one commit with ``git archive`` (every
file, dotfiles included), checked equal to that commit's tree blob for blob, smoked, swapped in by one rename, checked
again, and renamed back on any failure. The revision installed is recorded, so the tree always equals a named commit.
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
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_overlay_tree_install_effect.models.model_overlay_tree_install import (
    ModelOverlayCheck,
    ModelOverlayTreeInstallRequest,
    ModelOverlayTreeInstallResult,
)

IGNORE = ("__pycache__", ".in_use", "*.bak*", ".orphaned_at", "*.pyc", ".DS_Store")
ROLLBACK_TAG = ".rollback-"
FAILED_TAG = ".failed-"
DETAIL_MAX = 600


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def ignored(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(part, g) for part in rel.split("/") for g in IGNORE)


def tree_of(top: Path) -> dict[str, str]:
    """``{path: blob}`` of an installed tree; symlinks count as their target text."""
    out: dict[str, str] = {}
    for here, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d != ".git" and not ignored(d)]
        for name in files:
            full = os.path.join(here, name)
            if ignored(name):
                continue
            data = (
                os.readlink(full).encode()
                if os.path.islink(full)
                else Path(full).read_bytes()
            )
            out[os.path.relpath(full, top).replace(os.sep, "/")] = git_blob(data)
    return out


class HandlerOverlayTreeInstall:
    """``handle(request) -> result``."""

    def handle(
        self, request: ModelOverlayTreeInstallRequest
    ) -> ModelOverlayTreeInstallResult:
        if request.operation == "smoke":
            checks = self.smoke(Path(request.install_path), request.plugin_name)
            return self._result(request, "pass" if self._ok(checks) else "fail", checks)
        if request.operation == "verify":
            return self.verify(request)
        return self.install(request)

    # ------------------------------------------------------------------ install

    def install(
        self, r: ModelOverlayTreeInstallRequest
    ) -> ModelOverlayTreeInstallResult:
        dest = Path(r.install_path)
        stamp = self.stamp()
        staged = dest.parent / f".{dest.name}.staged-{stamp}"
        try:
            commit = self.resolve(Path(r.clone), r.ref)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shipped = self.build(Path(r.clone), commit, r.subdir, staged)
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
                    ModelOverlayCheck(
                        name="build", ok=False, detail=str(exc)[:DETAIL_MAX]
                    )
                ],
                detail="staged build failed; nothing installed",
            )
        before = [self.equal(staged, shipped), *self.smoke(staged, r.plugin_name)]
        if not self._ok(before) or r.dry_run:
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
        manifest = Path(r.installed_plugins) if r.installed_plugins else None
        manifest_before = (
            manifest.read_bytes()
            if manifest is not None and manifest.is_file()
            else None
        )
        if had:
            os.rename(dest, rollback)
        try:
            os.rename(staged, dest)
        except OSError as exc:
            if had:
                os.rename(rollback, dest)
            shutil.rmtree(staged, ignore_errors=True)
            return self._result(
                r,
                "refused",
                [*before, ModelOverlayCheck(name="swap", ok=False, detail=str(exc))],
                commit=commit,
                detail="swap failed; previous tree put back",
            )
        self.set_commit(manifest, dest, commit)
        after = [self.equal(dest, shipped), *self.smoke(dest, r.plugin_name)]
        record = self.load(r.record)
        if self._ok(after):
            trees = record.setdefault("trees", {})
            prior = trees.get(dest.name)
            if had and prior:
                trees[rollback.name] = prior
            trees[dest.name] = {"commit": commit, "at": stamp}
            record["last_good"] = {"path": dest.name, "commit": commit, "at": stamp}
            self.prune(dest, r.keep, record)
            self.save(r.record, record)
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
            os.rename(rollback, dest)
        if manifest is not None and manifest_before is not None:
            self.write_atomic(manifest, manifest_before)
        return self._result(
            r,
            "rolled-back" if had else "failed",
            after,
            commit=commit,
            restored=dest.name if had else "",
            detail=f"installed tree failed; kept as {failed.name}"
            + ("; previous tree back" if had else ""),
        )

    def verify(
        self, r: ModelOverlayTreeInstallRequest
    ) -> ModelOverlayTreeInstallResult:
        """Smoke the installed tree and, with a clone, check it equals the commit ``record`` holds for it."""
        dest = Path(r.install_path)
        current = str(
            ((self.load(r.record).get("trees") or {}).get(dest.name) or {}).get(
                "commit", ""
            )
        )
        checks = self.smoke(dest, r.plugin_name)
        if current and r.clone:
            try:
                checks.append(
                    self.equal(dest, self.ship(Path(r.clone), current, r.subdir))
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                checks.append(
                    ModelOverlayCheck(
                        name="tree-equal", ok=False, detail=str(exc)[:DETAIL_MAX]
                    )
                )
        elif not current:
            checks.append(
                ModelOverlayCheck(
                    name="recorded-revision",
                    ok=False,
                    detail=f"no revision recorded for {dest.name}",
                )
            )
        return self._result(
            r, "pass" if self._ok(checks) else "fail", checks, commit=current
        )

    # ------------------------------------------------------------------ build and compare

    @staticmethod
    def resolve(clone: Path, ref: str) -> str:
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
                timeout=30,
                check=False,
            )
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip()
        raise ValueError(f"{ref} is no commit in {clone}")

    @staticmethod
    def ship(clone: Path, commit: str, subdir: str) -> dict[str, str]:
        """``{path: blob}`` that installing ``subdir`` at ``commit`` ships."""
        prefix = subdir.strip("/") + "/"
        done = subprocess.run(
            ["git", "-C", str(clone), "ls-tree", "-r", "-z", commit, prefix],
            capture_output=True,
            timeout=60,
            check=True,
        )
        shipped: dict[str, str] = {}
        for entry in done.stdout.decode("utf-8", "replace").split("\0"):
            meta, _, path = entry.partition("\t")
            fields = meta.split()
            if path.startswith(prefix) and len(fields) == 3 and fields[1] == "blob":
                shipped[path[len(prefix) :]] = fields[2]
        if not shipped:
            raise ValueError(f"no {subdir} at {commit[:12]}")
        return shipped

    def build(
        self, clone: Path, commit: str, subdir: str, staged: Path
    ) -> dict[str, str]:
        shipped = self.ship(clone, commit, subdir)
        archive = subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                "archive",
                "--format=tar",
                commit,
                "--",
                subdir.strip("/"),
            ],
            capture_output=True,
            timeout=120,
            check=True,
        ).stdout
        work = Path(tempfile.mkdtemp(prefix=f".{staged.name}.x-", dir=staged.parent))
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(work, filter="tar")
            os.rename(work / subdir.strip("/"), staged)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return shipped

    @staticmethod
    def equal(root: Path, shipped: dict[str, str]) -> ModelOverlayCheck:
        have = tree_of(root)
        shipped = {p: b for p, b in shipped.items() if not ignored(p)}
        changed = sorted(p for p in have if p in shipped and have[p] != shipped[p])
        added = sorted(p for p in have if p not in shipped)
        removed = sorted(p for p in shipped if p not in have)
        if not (changed or added or removed):
            return ModelOverlayCheck(
                name="tree-equal", ok=True, detail=f"{len(have)} files"
            )
        first = (changed + added + removed)[0]
        return ModelOverlayCheck(
            name="tree-equal",
            ok=False,
            detail=f"changed={len(changed)} added={len(added)} removed={len(removed)} first={first}",
        )

    # ------------------------------------------------------------------ smoke

    @staticmethod
    def smoke(root: Path, plugin_name: str) -> list[ModelOverlayCheck]:
        """The suite, generated from the tree: the manifest names the plugin and each overlay file is a YAML mapping."""
        checks: list[ModelOverlayCheck] = []
        manifest = root / ".claude-plugin" / "plugin.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            ok = isinstance(data, dict) and data.get("name") == plugin_name
            checks.append(
                ModelOverlayCheck(
                    name="manifest",
                    ok=ok,
                    detail="" if ok else f"plugin.json name is not {plugin_name!r}",
                )
            )
        except (OSError, ValueError) as exc:
            checks.append(
                ModelOverlayCheck(
                    name="manifest", ok=False, detail=str(exc)[:DETAIL_MAX]
                )
            )
        files = sorted(root.glob("overlays/*/overlay.yaml"))
        if not files:
            checks.append(
                ModelOverlayCheck(
                    name="suite", ok=False, detail=f"no overlays under {root.name}"
                )
            )
        for path in files:
            rel = path.relative_to(root).as_posix()
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                ok = isinstance(loaded, dict) and bool(loaded)
                checks.append(
                    ModelOverlayCheck(
                        name=f"parses {rel}",
                        ok=ok,
                        detail="" if ok else "not a non-empty mapping",
                    )
                )
            except (OSError, yaml.YAMLError) as exc:
                checks.append(
                    ModelOverlayCheck(
                        name=f"parses {rel}", ok=False, detail=str(exc)[:DETAIL_MAX]
                    )
                )
        return checks

    # ------------------------------------------------------------------ records

    @staticmethod
    def stamp() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    @staticmethod
    def _ok(checks: list[ModelOverlayCheck]) -> bool:
        return all(c.ok for c in checks)

    @staticmethod
    def load(path: str) -> dict[str, Any]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def save(self, path: str, data: dict[str, Any]) -> None:
        if path:
            self.write_atomic(
                Path(path), (json.dumps(data, indent=1, sort_keys=True) + "\n").encode()
            )

    @staticmethod
    def write_atomic(dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)

    def set_commit(self, manifest: Path | None, dest: Path, commit: str) -> None:
        """Point the ``installed_plugins.json`` entry whose ``installPath`` is ``dest`` at ``commit``."""
        if manifest is None or not manifest.is_file():
            return
        data = json.loads(manifest.read_text(encoding="utf-8"))
        hit = False
        for entries in (data.get("plugins") or {}).values():
            for entry in entries if isinstance(entries, list) else []:
                if os.path.realpath(
                    str(entry.get("installPath") or "")
                ) == os.path.realpath(dest):
                    entry["gitCommitSha"], hit = commit, True
        if hit:
            self.write_atomic(manifest, (json.dumps(data, indent=2) + "\n").encode())

    @staticmethod
    def prune(dest: Path, keep: int, record: dict[str, Any]) -> None:
        for tag in (ROLLBACK_TAG, FAILED_TAG):
            for old in sorted(dest.parent.glob(f"{dest.name}{tag}*"), reverse=True)[
                max(keep, 1) :
            ]:
                shutil.rmtree(old, ignore_errors=True)
                (record.get("trees") or {}).pop(old.name, None)

    def _result(
        self,
        r: ModelOverlayTreeInstallRequest,
        verdict: str,
        checks: list[ModelOverlayCheck],
        **kw: str,
    ) -> ModelOverlayTreeInstallResult:
        return ModelOverlayTreeInstallResult(
            operation=r.operation,
            verdict=verdict,
            install_path=r.install_path,
            checks=tuple(checks),
            **kw,
        )
