# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

from scripts.install_codex_skills import install_skills


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

    actions = install_skills(source_dir, dest_dir, force=False)

    assert actions == ["skip session-bootstrap: destination exists"]
    assert existing.exists()
