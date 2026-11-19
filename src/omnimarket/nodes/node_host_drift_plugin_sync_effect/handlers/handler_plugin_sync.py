# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sync a data-only plugin to the merged tip of its repository by REVISION (OMN-20689).

onex-overlays has no tree installer: `claude plugin update` compares version strings, so an
unchanged version 0.1.30 read "latest" and never moved while the plugin sat 12 commits behind.
This builds the plugin's tree whole from one commit (`git archive <tip>:<plugin dir>`), checks its
manifest, swaps it in by rename keeping the previous tree, sets `gitCommitSha` in the installed
record, checks again and puts the previous tree back on any failure. An installed revision that
carries commits touching the plugin that the tip lacks is never dropped: the item stays alert only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.nodes.node_host_drift_plugin_sync_effect.models.model_plugin_sync import (
    EnumPluginSyncOutcome as Outcome,
)
from omnimarket.nodes.node_host_drift_plugin_sync_effect.models.model_plugin_sync import (
    ModelPluginSyncRequest,
    ModelPluginSyncResult,
    ModelPluginSyncSettings,
)

OVERLAY_PATH = Path(__file__).resolve().parents[1] / "sync_overlay.yaml"
DETAIL_LIMIT = 300
MANIFEST_SUFFIX = "/.claude-plugin/plugin.json"


def load_settings(path: Path = OVERLAY_PATH) -> ModelPluginSyncSettings:
    return ModelPluginSyncSettings.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


class ProtocolPluginSyncGit(Protocol):
    def run(self, args: Sequence[str]) -> tuple[int, str]: ...


class SubprocessPluginSyncGit:
    """The real backend: runs the argument vector and returns its exit code and combined output."""

    def run(self, args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=600
        )
        return done.returncode, (done.stdout + done.stderr)


class HandlerHostDriftPluginSync:
    """Effect: install a data-only plugin at the merged tip; handle(request) -> result."""

    def __init__(
        self,
        git: ProtocolPluginSyncGit | None = None,
        settings: ModelPluginSyncSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._git = git if git is not None else SubprocessPluginSyncGit()
        self._settings = settings if settings is not None else load_settings()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def handle(self, request: ModelPluginSyncRequest) -> ModelPluginSyncResult:
        s = self._settings
        if not request.manifest_path.endswith(MANIFEST_SUFFIX):
            return self._result(
                request, Outcome.ALERT_ONLY, f"manifest path is not {MANIFEST_SUFFIX}"
            )
        subdir = request.manifest_path.removesuffix(MANIFEST_SUFFIX)
        git = ["git", "-C", request.clone_path]
        if request.fetch:
            code, out = self._git.run([*git, "fetch", "--quiet", "origin", request.ref])
            if code:
                return self._result(
                    request,
                    Outcome.FAILED,
                    f"cannot fetch {request.ref}: {out[-DETAIL_LIMIT:]}",
                )
        code, tip_out = self._git.run(
            [*git, "rev-parse", "--verify", "-q", f"origin/{request.ref}"]
        )
        tip = tip_out.strip()
        if code or not re.fullmatch(s.sha_pattern, tip):
            return self._result(
                request, Outcome.FAILED, f"origin/{request.ref} does not resolve"
            )
        registry = Path(request.home) / s.registry_rel
        try:
            record = json.loads(registry.read_text(encoding="utf-8"))
            entry = record["plugins"][request.name][0]
            install_path = Path(entry["installPath"])
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return self._result(
                request,
                Outcome.ALERT_ONLY,
                f"{request.name} is not readable in the installed record",
            )
        revision = entry.get("gitCommitSha")
        revision = (
            revision
            if isinstance(revision, str) and re.fullmatch(s.sha_pattern, revision)
            else None
        )
        if revision == tip:
            return self._result(
                request,
                Outcome.ALREADY_AT_TIP,
                "installed revision is the merged tip",
                revision,
                tip,
            )
        if revision is not None:
            code, _ = self._git.run(
                [*git, "merge-base", "--is-ancestor", tip, revision]
            )
            if code == 0:
                return self._result(
                    request,
                    Outcome.ALREADY_AT_TIP,
                    "installed revision already holds the merged tip",
                    revision,
                    revision,
                )
            code, _ = self._git.run(
                [*git, "merge-base", "--is-ancestor", revision, tip]
            )
            if code == 1:
                code, listed = self._git.run(
                    [
                        *git,
                        "log",
                        "--no-merges",
                        "--max-count=20",
                        "--format=%h %s",
                        f"{tip}..{revision}",
                        "--",
                        subdir,
                    ]
                )
                lost = "; ".join(
                    line.strip() for line in listed.splitlines() if line.strip()
                )
                if not code and lost:
                    return self._result(
                        request,
                        Outcome.ALERT_ONLY,
                        f"installed revision {revision[:12]} carries commits touching {subdir} that "
                        f"origin/{request.ref} lacks: a tip install would drop them ({lost})",
                        revision,
                    )
        return self._swap(
            request, entry, record, registry, install_path, subdir, tip, revision
        )

    def _swap(
        self,
        request: ModelPluginSyncRequest,
        entry: dict[str, object],
        record: dict[str, object],
        registry: Path,
        install_path: Path,
        subdir: str,
        tip: str,
        revision: str | None,
    ) -> ModelPluginSyncResult:
        s = self._settings
        stage = install_path.with_name(
            f"{install_path.name}{s.staging_infix}{tip[:12]}"
        )
        archive = stage.with_name(stage.name + ".tar")
        shutil.rmtree(stage, ignore_errors=True)
        archive.unlink(missing_ok=True)
        try:
            code, out = self._git.run(
                [
                    "git",
                    "-C",
                    request.clone_path,
                    "archive",
                    "--format=tar",
                    "-o",
                    str(archive),
                    f"{tip}:{subdir}",
                ]
            )
            if code:
                return self._result(
                    request,
                    Outcome.FAILED,
                    f"cannot archive {subdir} at {tip[:12]}: {out[-DETAIL_LIMIT:]}",
                    revision,
                )
            stage.mkdir(parents=True)
            with tarfile.open(archive) as bundle:
                bundle.extractall(stage, filter="data")
            version = self._manifest_version(stage, request.name)
            if version is None:
                return self._result(
                    request,
                    Outcome.FAILED,
                    f"staged tree has no readable manifest for {request.name}",
                    revision,
                )
            target = install_path.with_name(version)
            rollback = None
            if target.exists():
                stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
                rollback = target.with_name(f"{target.name}{s.rollback_infix}{stamp}")
                target.rename(rollback)
            stage.rename(target)
            previous = json.dumps(record)
            entry.update(
                gitCommitSha=tip,
                installPath=str(target),
                version=version,
                lastUpdated=self._clock().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            )
            self._write_record(registry, record)
            if self._manifest_version(target, request.name) is None:
                self._write_record(registry, json.loads(previous))
                shutil.rmtree(target, ignore_errors=True)
                if rollback is not None:
                    rollback.rename(target)
                return self._result(
                    request,
                    Outcome.FAILED,
                    "installed tree failed its check; the previous tree is back",
                    revision,
                )
            self._prune(target, rollback)
            return self._result(
                request,
                Outcome.APPLIED,
                f"{request.name} synced to {tip[:12]} (version {version})",
                revision,
                tip,
            )
        except (OSError, tarfile.TarError) as exc:
            return self._result(
                request, Outcome.FAILED, f"sync failed: {exc}"[:DETAIL_LIMIT], revision
            )
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)

    def _manifest_version(self, tree: Path, name: str) -> str | None:
        try:
            manifest = json.loads(
                (tree / self._settings.manifest_rel).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        version = manifest.get("version") if isinstance(manifest, dict) else None
        if not isinstance(version, str) or not version or "/" in version:
            return None
        return version if manifest.get("name") == name.partition("@")[0] else None

    @staticmethod
    def _write_record(registry: Path, record: dict[str, object]) -> None:
        scratch = registry.with_name(registry.name + ".sync-tmp")
        scratch.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(scratch, registry)

    def _prune(self, target: Path, fresh: Path | None) -> None:
        infix = self._settings.rollback_infix
        kept = sorted(
            (
                p
                for p in target.parent.iterdir()
                if p.is_dir()
                and re.fullmatch(re.escape(target.name + infix) + r".+", p.name)
            ),
            key=lambda p: p.name,
        )
        for old in kept[: -self._settings.rollback_keep]:
            if old != fresh:
                shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _result(
        request: ModelPluginSyncRequest,
        outcome: Outcome,
        detail: str,
        previous: str | None = None,
        installed: str | None = None,
    ) -> ModelPluginSyncResult:
        return ModelPluginSyncResult(
            name=request.name,
            outcome=outcome,
            detail=detail,
            previous_revision=previous,
            installed_revision=installed,
        )
