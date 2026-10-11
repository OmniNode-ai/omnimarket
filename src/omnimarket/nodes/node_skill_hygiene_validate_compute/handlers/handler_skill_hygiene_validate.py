# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill directory hygiene over one skills-tree snapshot.

Seven rules, each with the severity of the change-control validator this node
replaces (``scripts/validation/validate_skill_hygiene.py``):

1. underscore-names      ERROR    skill and nested skill dir names use underscores, not dashes
2. no-duplicate-names    ERROR    no two top-level skill dirs normalize to one name (``-`` to ``_``)
3. no-unindexed-nesting  ERROR    a parent SKILL.md with child skills carries ``index: true``
4. skill-md-required     WARNING  every leaf skill dir has a SKILL.md (ERROR when strict)
5. name-matches-dir      WARNING  SKILL.md ``name:`` equals its dir name (ERROR when strict)
6. no-python-in-skills   ERROR    no ``.py`` file outside a ``_``-prefixed dir, except the root ``__init__.py``
7. no-orphan-topics      WARNING  no ``topics.yaml`` in a dir without a SKILL.md

Directories whose name starts with ``_`` are infrastructure and are skipped by
every rule. The snapshot comes from node_skill_tree_read_effect; this handler
does no I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath

from omnibase_core.enums.enum_severity import EnumSeverity

from omnimarket.models.skill_tree import (
    ModelSkillTreeChild,
    ModelSkillTreeEntry,
    ModelSkillTreeSnapshot,
)
from omnimarket.nodes.node_skill_hygiene_validate_compute.models import (
    EnumSkillHygieneCheck,
    ModelSkillHygieneValidateRequest,
    ModelSkillHygieneValidateResult,
    ModelSkillHygieneViolation,
)


def _is_infrastructure_dir(name: str) -> bool:
    return name.startswith("_")


def _violation(
    path: str, check: EnumSkillHygieneCheck, severity: EnumSeverity, message: str
) -> ModelSkillHygieneViolation:
    return ModelSkillHygieneViolation(
        path=path, check=check, severity=severity, message=message
    )


def _promoted(strict: bool) -> EnumSeverity:
    return EnumSeverity.ERROR if strict else EnumSeverity.WARNING


def _skill_entries(tree: ModelSkillTreeSnapshot) -> list[ModelSkillTreeEntry]:
    return [
        entry
        for entry in tree.entries
        if entry.is_dir and not _is_infrastructure_dir(entry.name)
    ]


def _child_skill_dirs(entry: ModelSkillTreeEntry) -> list[ModelSkillTreeChild]:
    return [
        child
        for child in entry.children
        if child.is_dir and not _is_infrastructure_dir(child.name)
    ]


def _parse_frontmatter(text: str | None) -> dict[str, str]:
    """Simple ``key: value`` pairs of the ``---`` delimited block; unreadable is empty."""
    result: dict[str, str] = {}
    if text is None:
        return result

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return result

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key and not value.startswith("{"):
                result[key] = value

    return result


def _check_underscore_names(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    violations: list[ModelSkillHygieneViolation] = []
    for entry in _skill_entries(tree):
        if "-" in entry.name:
            violations.append(
                _violation(
                    entry.name,
                    EnumSkillHygieneCheck.UNDERSCORE_NAMES,
                    EnumSeverity.ERROR,
                    f"Skill directory uses dashes: '{entry.name}' — rename to '{entry.name.replace('-', '_')}'",
                )
            )
        for child in _child_skill_dirs(entry):
            if "-" in child.name:
                violations.append(
                    _violation(
                        f"{entry.name}/{child.name}",
                        EnumSkillHygieneCheck.UNDERSCORE_NAMES,
                        EnumSeverity.ERROR,
                        f"Nested skill directory uses dashes: '{child.name}' — rename to '{child.name.replace('-', '_')}'",
                    )
                )
    return violations


def _check_no_duplicate_names(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    names: dict[str, list[str]] = {}
    for entry in _skill_entries(tree):
        names.setdefault(entry.name.replace("-", "_"), []).append(entry.name)
    return [
        _violation(
            ", ".join(originals),
            EnumSkillHygieneCheck.NO_DUPLICATE_NAMES,
            EnumSeverity.ERROR,
            f"Directories normalize to same name '{normalized}': {originals}",
        )
        for normalized, originals in sorted(names.items())
        if len(originals) > 1
    ]


def _has_child_skills(entry: ModelSkillTreeEntry) -> bool:
    return any(child.has_skill_md for child in _child_skill_dirs(entry))


def _check_no_unindexed_nesting(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    return [
        _violation(
            entry.name,
            EnumSkillHygieneCheck.NO_UNINDEXED_NESTING,
            EnumSeverity.ERROR,
            f"Parent dir '{entry.name}' has SKILL.md with child skill dirs but missing 'index: true' in frontmatter",
        )
        for entry in _skill_entries(tree)
        if entry.has_skill_md
        and _has_child_skills(entry)
        and _parse_frontmatter(entry.skill_md_text).get("index", "").lower() != "true"
    ]


def _check_skill_md_required(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    # An index parent's children are collected only when they hold a SKILL.md,
    # so only a leaf skill dir can be reported missing one.
    return [
        _violation(
            entry.name,
            EnumSkillHygieneCheck.SKILL_MD_REQUIRED,
            _promoted(strict),
            f"Skill directory '{entry.name}' is missing SKILL.md",
        )
        for entry in _skill_entries(tree)
        if not _has_child_skills(entry) and not entry.has_skill_md
    ]


def _name_mismatch(
    dir_name: str,
    display_name: str,
    has_skill_md: bool,
    skill_md_text: str | None,
    strict: bool,
) -> ModelSkillHygieneViolation | None:
    if not has_skill_md:
        return None
    name = _parse_frontmatter(skill_md_text).get("name", "")
    if not name or name == dir_name:
        return None
    return _violation(
        display_name,
        EnumSkillHygieneCheck.NAME_MATCHES_DIR,
        _promoted(strict),
        f"SKILL.md name: '{name}' does not match directory name '{dir_name}'",
    )


def _check_name_matches_dir(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    violations: list[ModelSkillHygieneViolation] = []
    for entry in _skill_entries(tree):
        found = _name_mismatch(
            entry.name, entry.name, entry.has_skill_md, entry.skill_md_text, strict
        )
        if found is not None:
            violations.append(found)
        for child in _child_skill_dirs(entry):
            found = _name_mismatch(
                child.name,
                f"{entry.name}/{child.name}",
                child.has_skill_md,
                child.skill_md_text,
                strict,
            )
            if found is not None:
                violations.append(found)
    return violations


def _under_infrastructure(rel: PurePath) -> bool:
    return any(_is_infrastructure_dir(part) for part in rel.parts[:-1])


def _check_no_python_in_skills(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    violations: list[ModelSkillHygieneViolation] = []
    for py_file in tree.python_files:
        rel = PurePath(py_file)
        if _under_infrastructure(rel) or rel.parts == ("__init__.py",):
            continue
        violations.append(
            _violation(
                py_file,
                EnumSkillHygieneCheck.NO_PYTHON_IN_SKILLS,
                EnumSeverity.ERROR,
                f"Python file in skill directory: {py_file} — move to a _-prefixed infrastructure dir (e.g. _lib/)",
            )
        )
    return violations


def _check_no_orphan_topics(
    tree: ModelSkillTreeSnapshot, strict: bool
) -> list[ModelSkillHygieneViolation]:
    violations: list[ModelSkillHygieneViolation] = []
    for topics_file in tree.topics_files:
        rel = PurePath(topics_file.path)
        if _under_infrastructure(rel) or topics_file.parent_has_skill_md:
            continue
        parent_name = (PurePath(tree.skills_root) / rel).parent.name
        violations.append(
            _violation(
                topics_file.path,
                EnumSkillHygieneCheck.NO_ORPHAN_TOPICS,
                EnumSeverity.WARNING,
                f"topics.yaml found without SKILL.md in same directory: {parent_name}/",
            )
        )
    return violations


_CHECKS: tuple[
    Callable[[ModelSkillTreeSnapshot, bool], list[ModelSkillHygieneViolation]], ...
] = (
    _check_underscore_names,
    _check_no_duplicate_names,
    _check_no_unindexed_nesting,
    _check_skill_md_required,
    _check_name_matches_dir,
    _check_no_python_in_skills,
    _check_no_orphan_topics,
)


class HandlerSkillHygieneValidate:
    """Stateless, pure hygiene verdict over the request's skills-tree snapshot."""

    def handle(
        self, request: ModelSkillHygieneValidateRequest
    ) -> ModelSkillHygieneValidateResult:
        violations = tuple(
            violation
            for check in _CHECKS
            for violation in check(request.tree, request.strict)
        )
        errors = sum(v.severity == EnumSeverity.ERROR for v in violations)
        return ModelSkillHygieneValidateResult(
            skills_root=request.tree.skills_root,
            strict=request.strict,
            violations=violations,
            error_count=errors,
            warning_count=sum(v.severity == EnumSeverity.WARNING for v in violations),
            passed=errors == 0,
        )


__all__ = ["HandlerSkillHygieneValidate"]
