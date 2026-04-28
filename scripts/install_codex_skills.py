# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_source_dir() -> Path:
    return _repo_root() / "plugins" / "onex" / "skills"


def _iter_skill_dirs(source_dir: Path) -> list[Path]:
    return sorted(path for path in source_dir.iterdir() if path.is_dir())


def install_skills(source_dir: Path, dest_dir: Path, force: bool = False) -> list[str]:
    source_dir = source_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    actions: list[str] = []
    for skill_dir in _iter_skill_dirs(source_dir):
        dest_path = dest_dir / skill_dir.name

        if dest_path.exists() or dest_path.is_symlink():
            if not force:
                actions.append(f"skip {skill_dir.name}: destination exists")
                continue
            if dest_path.is_symlink() or dest_path.is_file():
                dest_path.unlink()
            else:
                shutil.rmtree(dest_path)

        dest_path.symlink_to(skill_dir)
        actions.append(f"link {skill_dir.name} -> {skill_dir}")

    return actions


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install ONEX Codex skills into ~/.codex/skills via symlinks."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=_default_source_dir(),
        help="Directory containing skill subdirectories",
    )
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=Path.home() / ".codex" / "skills",
        help="Codex skills directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing destination entries",
    )
    args = parser.parse_args()

    actions = install_skills(args.source_dir, args.dest_dir, force=args.force)
    for line in actions:
        print(line)


if __name__ == "__main__":
    main()
