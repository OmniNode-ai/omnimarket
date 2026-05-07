# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_repo_source_dir() -> Path:
    return _repo_root() / "plugins" / "onex" / "skills"


def _default_codex_home() -> Path:
    return Path.home() / ".codex"


def _default_marketplace_source_dir(codex_home: Path) -> Path:
    return (
        codex_home
        / ".tmp"
        / "marketplaces"
        / "omninode-tools"
        / "plugins"
        / "onex"
        / "skills"
    )


def _iter_skill_dirs(source_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in source_dir.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def resolve_source_dir(
    *,
    source: str,
    codex_home: Path,
    explicit_source_dir: Path | None = None,
) -> Path:
    if explicit_source_dir is not None:
        source_dir = explicit_source_dir.resolve()
    elif source == "repo":
        source_dir = _default_repo_source_dir().resolve()
    elif source == "marketplace":
        source_dir = _default_marketplace_source_dir(codex_home).resolve()
    else:
        marketplace_dir = _default_marketplace_source_dir(codex_home)
        source_dir = (
            marketplace_dir.resolve()
            if marketplace_dir.is_dir()
            else _default_repo_source_dir().resolve()
        )

    if not source_dir.is_dir():
        raise FileNotFoundError(f"skill source directory not found: {source_dir}")

    return source_dir


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
        "--source",
        choices=("auto", "repo", "marketplace"),
        default="auto",
        help="Select the skill source. 'auto' prefers marketplace-sync when present.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=_default_codex_home(),
        help="Codex home used for marketplace-sync and default install destination",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Override the skill source directory explicitly",
    )
    parser.add_argument(
        "--dest-dir",
        type=Path,
        help="Codex skills directory override",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing destination entries",
    )
    args = parser.parse_args()

    source_dir = resolve_source_dir(
        source=args.source,
        codex_home=args.codex_home,
        explicit_source_dir=args.source_dir,
    )
    dest_dir = args.dest_dir or (args.codex_home / "skills")

    print(f"source {source_dir}")
    print(f"dest {dest_dir.resolve()}")

    actions = install_skills(source_dir, dest_dir, force=args.force)
    for line in actions:
        print(line)


if __name__ == "__main__":
    main()
