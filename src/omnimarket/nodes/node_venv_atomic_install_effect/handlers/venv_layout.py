# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read what a venv holds: distributions, their versions and the units they own.

A unit is the smallest thing exchanged in place: a top-level entry of
site-packages, or one file when that top-level directory is shared by several
distributions, or a file outside site-packages such as a console script.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..models.model_venv_atomic_install import ModelLockedPackage

SITE = "site:"
VENV = "venv:"
SCRIPTS_DIR = "bin"


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Distribution:
    name: str
    version: str
    dist_info: Path


def scan_distributions(site_packages: Path) -> dict[str, Distribution]:
    found: dict[str, Distribution] = {}
    with os.scandir(site_packages) as entries:
        for entry in entries:
            if not entry.name.endswith(".dist-info"):
                continue
            stem = entry.name.removesuffix(".dist-info")
            name, _, version = stem.rpartition("-")
            if name:
                found[normalize(name)] = Distribution(
                    name=normalize(name), version=version, dist_info=Path(entry.path)
                )
    return found


def installed_commit(dist: Distribution) -> str | None:
    """The commit a git-sourced distribution was installed from, if it was."""
    direct_url = dist.dist_info / "direct_url.json"
    if not direct_url.is_file():
        return None
    vcs = json.loads(direct_url.read_text(encoding="utf-8")).get("vcs_info", {})
    commit = vcs.get("commit_id")
    return str(commit) if commit else None


def read_lock(lock_file: Path) -> tuple[ModelLockedPackage, ...]:
    """Registry packages the lock pins; editable and workspace sources are not installs."""
    data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    return tuple(
        ModelLockedPackage(name=normalize(pkg["name"]), version=pkg["version"])
        for pkg in data.get("package", [])
        if "registry" in pkg.get("source", {}) and "version" in pkg
    )


def record_paths(dist_info: Path) -> list[str]:
    lines = (dist_info / "RECORD").read_text(encoding="utf-8").splitlines()
    return [line.rsplit(",", 2)[0].strip('"') for line in lines if line.strip()]


def unit_key(record_path: str, site_packages: Path, venv_root: Path) -> str | None:
    """Unit key of one live RECORD entry; None for bytecode, which is never owned."""
    parts = record_path.split("/")
    if "__pycache__" in parts:
        return None
    if parts[0] != "..":
        return SITE + parts[0] if len(parts) > 1 else SITE + record_path
    outside = Path(os.path.normpath(site_packages / record_path))
    return VENV + outside.relative_to(venv_root).as_posix()


def staged_unit_key(record_path: str) -> str | None:
    """Unit key of one RECORD entry of a ``--target`` install into the staging dir."""
    parts = record_path.split("/")
    if "__pycache__" in parts:
        return None
    if parts[0] == SCRIPTS_DIR and len(parts) > 1:
        return VENV + record_path
    return SITE + parts[0]


def live_path(key: str, site_packages: Path, venv_root: Path) -> Path:
    return (
        site_packages / key[len(SITE) :]
        if key.startswith(SITE)
        else venv_root / key[len(VENV) :]
    )


def stage_path(key: str, staging: Path) -> Path:
    return (
        staging / key[len(SITE) :]
        if key.startswith(SITE)
        else staging / key[len(VENV) :]
    )
