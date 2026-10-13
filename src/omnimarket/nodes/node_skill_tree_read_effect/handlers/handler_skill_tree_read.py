# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a skills tree into the snapshot the skill validators judge.

This is the only disk access of skill validation: the hygiene and contract
compute nodes are pure over what it returns. It reads, never writes, and makes
the same filesystem queries the change-control validators made, in the same
order: the sorted root listing, each top-level directory's sorted listing,
SKILL.md and prompt.md presence and text, every ``*.py`` and every
``topics.yaml`` found by ``rglob``. It skips exactly what the validators skip:
a ``_``-prefixed entry of the root or of a skill directory is neither listed
nor read, and no ``*.py`` or ``topics.yaml`` below a ``_``-prefixed directory
is recorded.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.skill_tree import (
    ModelSkillTreeChild,
    ModelSkillTreeEntry,
    ModelSkillTreeSnapshot,
    ModelSkillTreeTopicsFile,
    is_infrastructure_name,
    is_under_infrastructure,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

_SKILL_MD = "SKILL.md"
_PROMPT_MD = "prompt.md"


def _listing(path: Path) -> list[Path]:
    return [
        child
        for child in sorted(path.iterdir())
        if not is_infrastructure_name(child.name)
    ]


def _judged(root: Path, pattern: str) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob(pattern))
        if not is_under_infrastructure(str(path.relative_to(root)))
    ]


def _text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _child(path: Path) -> ModelSkillTreeChild:
    is_dir = path.is_dir()
    skill_md = path / _SKILL_MD
    return ModelSkillTreeChild(
        name=path.name,
        is_dir=is_dir,
        has_skill_md=is_dir and skill_md.exists(),
        skill_md_text=_text(skill_md) if is_dir else None,
    )


def _entry(path: Path) -> ModelSkillTreeEntry:
    if not path.is_dir():
        return ModelSkillTreeEntry(
            name=path.name, is_dir=False, has_skill_md=False, has_prompt_md=False
        )
    skill_md = path / _SKILL_MD
    prompt_md = path / _PROMPT_MD
    return ModelSkillTreeEntry(
        name=path.name,
        is_dir=True,
        has_skill_md=skill_md.exists(),
        has_prompt_md=prompt_md.exists(),
        skill_md_text=_text(skill_md),
        prompt_md_text=_text(prompt_md),
        children=tuple(_child(child) for child in _listing(path)),
    )


class HandlerSkillTreeRead:
    """Stateless, read-only snapshot of the request's skills tree."""

    def handle(self, request: ModelSkillTreeReadRequest) -> ModelSkillTreeSnapshot:
        root = request.skills_root
        if not root.is_dir():
            raise NotADirectoryError(f"skills root not found: {root}")
        return ModelSkillTreeSnapshot(
            skills_root=str(root),
            entries=tuple(_entry(entry) for entry in _listing(root)),
            python_files=tuple(
                str(path.relative_to(root)) for path in _judged(root, "*.py")
            ),
            topics_files=tuple(
                ModelSkillTreeTopicsFile(
                    path=str(path.relative_to(root)),
                    parent_has_skill_md=(path.parent / _SKILL_MD).exists(),
                )
                for path in _judged(root, "topics.yaml")
            ),
        )


__all__ = ["HandlerSkillTreeRead"]
