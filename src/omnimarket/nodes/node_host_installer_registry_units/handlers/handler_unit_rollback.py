# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The previous install of a unit, kept one level deep so a rollback can put it back (OMN-24108).

An install that replaces a unit's files first copies each of them to ``<state>/<unit>.previous/`` with a manifest
naming where each one came from and whether it existed. A rollback copies them back, or removes the unit's files
when the manifest says the unit had none (the install was the first). One level only: a rollback consumes the
kept copy, and an install that would write the text the unit already has keeps nothing, so a repeated install
never overwrites the good previous copy with the one being replaced.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

MANIFEST = "manifest.json"
KEPT_SUFFIX = ".previous"


class NoPreviousInstallError(ValueError):
    """A rollback was asked for a unit that has no kept previous install."""


def kept_directory(state: Path, unit: str) -> Path:
    return state / f"{unit}{KEPT_SUFFIX}"


def keep_previous(state: Path, unit: str, targets: dict[str, Path]) -> Path:
    """Copy each target that exists to the unit's kept directory, replacing any earlier kept copy."""
    kept = kept_directory(state, unit)
    shutil.rmtree(kept, ignore_errors=True)
    kept.mkdir(parents=True)
    manifest: dict[str, dict[str, object]] = {}
    for key, target in targets.items():
        present = target.exists()
        manifest[key] = {"target": str(target), "present": present}
        if not present:
            continue
        if target.is_dir():
            shutil.copytree(target, kept / key, symlinks=True)
        else:
            shutil.copy2(target, kept / key)
    (kept / MANIFEST).write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return kept


def read_previous(state: Path, unit: str) -> tuple[Path, dict[str, dict[str, object]]]:
    kept = kept_directory(state, unit)
    manifest = kept / MANIFEST
    if not manifest.is_file():
        raise NoPreviousInstallError(
            f"no previous install of {unit} is kept under {kept}; nothing to roll back "
            "(an install keeps the files it replaces, one level deep, and a rollback uses them up)"
        )
    return kept, json.loads(manifest.read_text(encoding="utf-8"))


def restore_previous(kept: Path, manifest: dict[str, dict[str, object]]) -> list[str]:
    """Put each kept file back where it came from; remove the ones the unit did not have. Returns what was done."""
    done: list[str] = []
    for key, row in sorted(manifest.items()):
        target = Path(str(row["target"]))
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        if row["present"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            source = kept / key
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
            done.append(f"restored {target}")
        else:
            done.append(
                f"removed {target} (the install that was rolled back was the first)"
            )
    shutil.rmtree(kept)
    return done
