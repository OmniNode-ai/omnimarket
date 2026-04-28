# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.install_codex_skills import install_skills, resolve_source_dir


def test_install_codex_skills_symlinks_each_skill(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    for name in ("aislop-sweep", "merge-sweep"):
        skill_dir = source_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n")

    actions = install_skills(source_dir, dest_dir)

    assert actions == [
        f"link aislop-sweep -> {(source_dir / 'aislop-sweep').resolve()}",
        f"link merge-sweep -> {(source_dir / 'merge-sweep').resolve()}",
    ]
    assert (dest_dir / "aislop-sweep").is_symlink()
    assert (dest_dir / "merge-sweep").is_symlink()


def test_install_codex_skills_skips_existing_without_force(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    skill_dir = source_dir / "session-bootstrap"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# session-bootstrap\n")
    existing = dest_dir / "session-bootstrap"
    existing.mkdir(parents=True)
    (existing / "sentinel.txt").write_text("keep", encoding="utf-8")

    actions = install_skills(source_dir, dest_dir, force=False)

    assert actions == ["skip session-bootstrap: destination exists"]
    assert existing.is_dir()
    assert not existing.is_symlink()
    assert (existing / "sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_install_codex_skills_replaces_existing_with_force(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    skill_dir = source_dir / "session-bootstrap"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# session-bootstrap\n")
    existing = dest_dir / "session-bootstrap"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("# stale\n")

    actions = install_skills(source_dir, dest_dir, force=True)

    assert actions == [f"link session-bootstrap -> {skill_dir.resolve()}"]
    assert existing.is_symlink()
    assert existing.resolve() == skill_dir.resolve()


def test_resolve_source_dir_prefers_marketplace_when_present(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    marketplace_dir = (
        codex_home
        / ".tmp"
        / "marketplaces"
        / "omninode-tools"
        / "plugins"
        / "onex"
        / "skills"
    )
    marketplace_dir.mkdir(parents=True)

    resolved = resolve_source_dir(source="auto", codex_home=codex_home)

    assert resolved == marketplace_dir.resolve()


def test_resolve_source_dir_uses_explicit_source_dir(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    explicit_source = tmp_path / "custom-skills"
    explicit_source.mkdir(parents=True)

    resolved = resolve_source_dir(
        source="marketplace",
        codex_home=codex_home,
        explicit_source_dir=explicit_source,
    )

    assert resolved == explicit_source.resolve()


def test_resolve_source_dir_marketplace_requires_synced_tree(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"

    with pytest.raises(FileNotFoundError, match="skill source directory not found"):
        resolve_source_dir(source="marketplace", codex_home=codex_home)
