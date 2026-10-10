# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read-only shape of a skills tree, as the skill validators see it.

node_skill_tree_read_effect reads one from disk; the skill validator compute
nodes are pure over it. Every sequence keeps the order the reader produced
(``sorted`` over ``Path`` objects), which is the order violations are reported in.
"""

from pydantic import BaseModel, ConfigDict


class ModelSkillTreeChild(BaseModel):
    """An entry directly under a top-level skills-root entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    is_dir: bool
    has_skill_md: bool
    # Text of SKILL.md when it is a regular file; None when absent or not a file.
    skill_md_text: str | None = None


class ModelSkillTreeEntry(BaseModel):
    """An entry directly under the skills root."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    is_dir: bool
    has_skill_md: bool
    has_prompt_md: bool
    skill_md_text: str | None = None
    prompt_md_text: str | None = None
    # Entries of a directory, in sorted order; empty for a non-directory.
    children: tuple[ModelSkillTreeChild, ...] = ()


class ModelSkillTreeTopicsFile(BaseModel):
    """A ``topics.yaml`` anywhere under the skills root."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    parent_has_skill_md: bool


class ModelSkillTreeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # The root as the caller named it, for reports.
    skills_root: str
    entries: tuple[ModelSkillTreeEntry, ...]
    # Every path matching ``*.py`` at any depth, relative to the root.
    python_files: tuple[str, ...]
    topics_files: tuple[ModelSkillTreeTopicsFile, ...]
