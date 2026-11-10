# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install only the moved packages into a staging directory and exchange them in place.

The old additive pass cloned the whole live venv to change a few packages. This
handler reads the locked versions against the installed ones, installs only the
packages that moved into a staging directory, then exchanges each package's
units with the live ones, journalling every step first. A failure at any point
restores every exchanged unit from the journal, so the live venv lists the same
files it listed before the pass.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from ..models.model_venv_atomic_install import (
    EnumVenvInstallStatus,
    ModelLockedPackage,
    ModelMovedPackage,
    ModelVenvAtomicInstallRequest,
    ModelVenvAtomicInstallResult,
)
from .venv_exchange import Exchange, Journal, exchange_paths, roll_back
from .venv_layout import (
    SITE,
    Distribution,
    installed_commit,
    live_path,
    normalize,
    read_lock,
    record_paths,
    scan_distributions,
    stage_path,
    staged_unit_key,
    unit_key,
)

Installer = Callable[[ModelVenvAtomicInstallRequest, list[str]], None]
GRAVEYARD = ".removed"
RELOCATABLE_INTERPRETER = re.compile(
    r"(\"\$\(dirname -- \"\$\(realpath -- \"\$0\"\)\"\)\"/)'[^']+'"
)


class VenvInstallError(RuntimeError):
    """The pass could not complete; the message names what stopped it."""


def install_with_uv(
    request: ModelVenvAtomicInstallRequest, requirements: list[str]
) -> None:
    if not request.installer_command or request.python is None:
        raise VenvInstallError("installer_command and python are required to install")
    command = [
        *request.installer_command,
        "pip",
        "install",
        "--no-deps",
        "--python",
        str(request.python),
        "--target",
        str(request.staging_dir),
        *requirements,
    ]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise VenvInstallError(
            f"installer exited {done.returncode}: {done.stderr.strip()[-400:]}"
        )


def relocate_scripts(staging: Path, interpreter_name: str) -> None:
    """Make staged console scripts name the interpreter the way the live ones do."""
    scripts = staging / "bin"
    if not scripts.is_dir():
        return
    for script in scripts.iterdir():
        if not script.is_file() or script.is_symlink():
            continue
        try:
            text = script.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rewritten = RELOCATABLE_INTERPRETER.sub(
            rf"\1'{interpreter_name}'", text, count=1
        )
        if rewritten != text:
            script.write_text(rewritten, encoding="utf-8")


class HandlerVenvAtomicInstall:
    def __init__(
        self,
        *,
        installer: Installer = install_with_uv,
        exchange: Exchange = exchange_paths,
    ) -> None:
        self.installer = installer
        self.exchange = exchange

    def handle(
        self, request: ModelVenvAtomicInstallRequest
    ) -> ModelVenvAtomicInstallResult:
        journal = Journal(request.journal_path)
        recovered = self._recover(journal, request.staging_dir)
        locked = request.locked or (
            read_lock(request.lock_file) if request.lock_file is not None else ()
        )
        installed = scan_distributions(request.site_packages)
        by_name = {normalize(lp.name): lp for lp in locked}
        moved = tuple(
            ModelMovedPackage(
                name=name,
                installed=installed[name].version,
                locked=lp.version,
                commit=lp.commit,
            )
            for name, lp in by_name.items()
            if name in installed and self._has_moved(installed[name], lp)
        )
        unchanged = len(installed) - len(moved)
        if not moved:
            return ModelVenvAtomicInstallResult(
                status=EnumVenvInstallStatus.NOOP,
                unchanged=unchanged,
                recovered_units=recovered,
            )
        wanted = {m.name for m in moved}
        requirements = [by_name[name].requirement for name in sorted(wanted)]
        try:
            self._stage(request, requirements)
        except (OSError, VenvInstallError) as exc:
            shutil.rmtree(request.staging_dir, ignore_errors=True)
            return ModelVenvAtomicInstallResult(
                status=EnumVenvInstallStatus.FAILED_BEFORE_EXCHANGE,
                moved=moved,
                unchanged=unchanged,
                recovered_units=recovered,
                reason=str(exc),
            )
        exchanged: list[str] = []
        try:
            self._exchange_all(request, installed, wanted, journal, exchanged)
            self._readback(request, {m.name: (m.locked, m.commit) for m in moved})
        except (OSError, VenvInstallError) as exc:
            restored = roll_back(journal, self.exchange)
            journal.discard()
            shutil.rmtree(request.staging_dir, ignore_errors=True)
            return ModelVenvAtomicInstallResult(
                status=EnumVenvInstallStatus.ROLLED_BACK,
                moved=moved,
                unchanged=unchanged,
                exchanged_units=tuple(exchanged),
                restored_units=tuple(restored),
                recovered_units=recovered,
                reason=str(exc),
            )
        journal.discard()
        shutil.rmtree(request.staging_dir, ignore_errors=True)
        return ModelVenvAtomicInstallResult(
            status=EnumVenvInstallStatus.INSTALLED,
            moved=moved,
            unchanged=unchanged,
            exchanged_units=tuple(exchanged),
            recovered_units=recovered,
        )

    def _recover(self, journal: Journal, staging: Path) -> tuple[str, ...]:
        """A journal left by a pass that died is undone before anything else runs."""
        if not journal.path.exists():
            return ()
        restored = roll_back(journal, self.exchange)
        journal.discard()
        shutil.rmtree(staging, ignore_errors=True)
        return tuple(restored)

    def _stage(
        self, request: ModelVenvAtomicInstallRequest, requirements: list[str]
    ) -> None:
        shutil.rmtree(request.staging_dir, ignore_errors=True)
        request.staging_dir.mkdir(parents=True)
        if os.stat(request.staging_dir).st_dev != os.stat(request.site_packages).st_dev:
            raise VenvInstallError(
                "staging dir must be on the same filesystem as the venv"
            )
        self.installer(request, requirements)
        if request.python is not None:
            relocate_scripts(request.staging_dir, request.python.name)

    def _units(
        self,
        request: ModelVenvAtomicInstallRequest,
        dist: Distribution,
        shared: set[str],
        *,
        staged: bool,
    ) -> list[str]:
        keys: list[str] = []
        for path in record_paths(dist.dist_info):
            key = (
                staged_unit_key(path)
                if staged
                else unit_key(path, request.site_packages, request.venv_root)
            )
            if key is None:
                continue
            if key.startswith(SITE) and key[len(SITE) :] in shared and "/" in path:
                key = SITE + path
            if key not in keys:
                keys.append(key)
        return keys

    def _exchange_all(
        self,
        request: ModelVenvAtomicInstallRequest,
        installed: dict[str, Distribution],
        wanted: set[str],
        journal: Journal,
        exchanged: list[str],
    ) -> None:
        owners: Counter[str] = Counter()
        for dist in installed.values():
            owners.update(
                {
                    k[len(SITE) :]
                    for k in self._units(request, dist, set(), staged=False)
                    if k.startswith(SITE)
                }
            )
        shared = {top for top, count in owners.items() if count > 1}
        staged = scan_distributions(request.staging_dir)
        site, root, staging = (
            request.site_packages,
            request.venv_root,
            request.staging_dir,
        )
        graveyard = staging / GRAVEYARD
        removals: list[str] = []
        for name in sorted(wanted):
            if name not in staged:
                raise VenvInstallError(f"installer did not stage {name}")
            old_units = self._units(request, installed[name], shared, staged=False)
            new_units = self._units(request, staged[name], shared, staged=True)
            for key in new_units:
                live, stage = live_path(key, site, root), stage_path(key, staging)
                self._ensure_parents(live.parent, site, journal)
                if os.path.lexists(live):
                    journal.record(
                        op="exchange",
                        unit=key,
                        live=str(live),
                        stage=str(stage),
                        live_ino=os.lstat(live).st_ino,
                    )
                    self.exchange(live, stage)
                else:
                    journal.record(op="add", unit=key, live=str(live), stage=str(stage))
                    live.parent.mkdir(parents=True, exist_ok=True)
                    stage.rename(live)
                exchanged.append(key)
            removals.extend(key for key in old_units if key not in new_units)
        for index, key in enumerate(removals):
            live, grave = live_path(key, site, root), graveyard / str(index)
            if not os.path.lexists(live):
                continue
            graveyard.mkdir(exist_ok=True)
            journal.record(op="remove", unit=key, live=str(live), stage=str(grave))
            live.rename(grave)
            exchanged.append(key)

    def _ensure_parents(self, parent: Path, site: Path, journal: Journal) -> None:
        missing: list[Path] = []
        while not parent.exists() and parent != site:
            missing.append(parent)
            parent = parent.parent
        for path in reversed(missing):
            journal.record(op="mkdir", path=str(path))
            path.mkdir()

    @staticmethod
    def _has_moved(dist: Distribution, locked: ModelLockedPackage) -> bool:
        if dist.version != locked.version:
            return True
        return locked.commit is not None and installed_commit(dist) != locked.commit

    def _readback(
        self,
        request: ModelVenvAtomicInstallRequest,
        want: dict[str, tuple[str, str | None]],
    ) -> None:
        now = scan_distributions(request.site_packages)
        wrong = {
            name: now[name].version if name in now else None
            for name, (version, commit) in want.items()
            if name not in now
            or now[name].version != version
            or (commit is not None and installed_commit(now[name]) != commit)
        }
        if wrong:
            raise VenvInstallError(
                f"readback does not show the locked versions: {wrong}"
            )
