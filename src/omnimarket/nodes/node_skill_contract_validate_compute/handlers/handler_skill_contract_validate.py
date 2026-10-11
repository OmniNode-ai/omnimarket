# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill contract parity over one skills tree.

Five rules over every top-level skill dir not prefixed with ``_``, each with the
severity of the change-control validator this node replaces
(``scripts/validation/validate_skill_contracts.py``):

1. args-parity            ERROR    every ``--flag`` arg in SKILL.md frontmatter appears in prompt.md
                          WARNING  SKILL.md declares ``--flag`` args but there is no prompt.md
2. sub-skill-exists       ERROR    ``Skill(skill="onex:X")`` and ``/onex:X`` refs in prompt.md
                                   resolve to ``X/`` (or ``X`` with ``-`` as ``_``) holding a SKILL.md
3. sub-skill-args         WARNING  flags dispatched with ``Skill(skill="onex:X", args="...")`` are
                                   declared in X's SKILL.md args
4. duplicate-frontmatter  ERROR    no top-level key appears twice in SKILL.md frontmatter
5. spec-prompt-predicates WARNING  backtick-quoted identifiers (3+ chars) on SKILL.md body lines
                                   naming a status, result, predicate, state, values, emit or
                                   output appear in prompt.md (ERROR when strict)

The snapshot comes from node_skill_tree_read_effect; this handler does no I/O.
"""

from __future__ import annotations

import re

from omnibase_core.enums.enum_severity import EnumSeverity

from omnimarket.models.skill_tree import ModelSkillTreeEntry, ModelSkillTreeSnapshot
from omnimarket.nodes.node_skill_contract_validate_compute.models import (
    EnumSkillContractCheck,
    ModelSkillContractValidateRequest,
    ModelSkillContractValidateResult,
    ModelSkillContractViolation,
)

_STRICT_PROMOTED = frozenset({EnumSkillContractCheck.SPEC_PROMPT_PREDICATES})

# --flag style args in prompt text
_RE_FLAG_IN_TEXT = re.compile(r"--[a-z][-a-z0-9]*")

# Skill(skill="onex:...") or /onex:... references
_RE_SUB_SKILL_REF = re.compile(
    r'(?:Skill\s*\(\s*skill\s*=\s*["\']onex:([a-z][-a-z0-9_]*)["\']'
    r"|/onex:([a-z][-a-z0-9_]*))"
)

# Skill(skill="onex:...", args="...") with the args string captured
_RE_SUB_SKILL_DISPATCH = re.compile(
    r'Skill\s*\(\s*skill\s*=\s*["\']onex:([a-z][-a-z0-9_]*)["\']'
    r'[^)]*args\s*=\s*["\']([^"\']*)["\']'
)

# Backtick-quoted identifiers in the SKILL.md body
_RE_BACKTICK_PREDICATE = re.compile(r"`([a-z][a-z0-9_]*(?:[-][a-z0-9_]+)*)`")

# Only body lines carrying one of these words are checked for predicates
_PREDICATE_CONTEXT_KEYWORDS = frozenset(
    {"status", "result", "predicate", "state", "values", "emit", "output"}
)


def _text(text: str | None, what: str, entry: ModelSkillTreeEntry) -> str:
    """The text of a file the snapshot says exists; one that is not a readable file is refused."""
    if text is None:
        raise ValueError(f"{entry.name}/{what} exists but is not a readable file")
    return text


def _violation(
    entry: ModelSkillTreeEntry,
    check: EnumSkillContractCheck,
    severity: EnumSeverity,
    message: str,
) -> ModelSkillContractViolation:
    return ModelSkillContractViolation(
        path=entry.name, check=check, severity=severity, message=message
    )


def _parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """Top-level frontmatter keys; an indented block under a key is kept as raw lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    frontmatter_lines: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        frontmatter_lines.append(line)

    result: dict[str, str | list[str]] = {}
    current_key: str | None = None
    current_list: list[str] = []

    for line in frontmatter_lines:
        if not line.startswith(" ") and not line.startswith("\t") and ":" in line:
            if current_key and current_list:
                result[current_key] = current_list
                current_list = []
            key, _, value = line.partition(":")
            current_key = key.strip()
            value = value.strip()
            if value:
                result[current_key] = value
        elif current_key and (line.startswith("  ") or line.startswith("\t")):
            current_list.append(line)

    if current_key and current_list:
        result[current_key] = current_list

    return result


def _frontmatter_flags(frontmatter: dict[str, str | list[str]]) -> list[str]:
    """``--flag`` names of the ``args:`` block; positional names are not checked."""
    args_raw = frontmatter.get("args")
    if not args_raw or not isinstance(args_raw, list):
        return []

    flags: list[str] = []
    for raw in args_raw:
        line = raw.strip()
        if line.startswith("- name:"):
            name = line.split(":", 1)[1].strip()
            if name.startswith("--"):
                flags.append(name)
        elif line.startswith("- --"):
            match = _RE_FLAG_IN_TEXT.search(line)
            if match:
                flags.append(match.group())
    return flags


def _sub_skill_refs(prompt_text: str) -> list[str]:
    refs: list[str] = []
    for match in _RE_SUB_SKILL_REF.finditer(prompt_text):
        ref = match.group(1) or match.group(2)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _sub_skill_dispatches(prompt_text: str) -> list[tuple[str, list[str]]]:
    return [
        (match.group(1), _RE_FLAG_IN_TEXT.findall(match.group(2)))
        for match in _RE_SUB_SKILL_DISPATCH.finditer(prompt_text)
    ]


def _skill_body(text: str) -> str:
    """SKILL.md after its frontmatter; the whole text when the block never closes."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :])
    return text


def _resolve_skill(
    tree: ModelSkillTreeSnapshot, ref: str
) -> ModelSkillTreeEntry | None:
    """The skills-root entry ``ref`` names (``-`` read as ``_`` first) when it holds a SKILL.md."""
    by_name = {entry.name: entry for entry in tree.entries}
    for candidate_name in (ref.replace("-", "_"), ref):
        candidate = by_name.get(candidate_name)
        if candidate is not None and candidate.has_skill_md:
            return candidate
    return None


def _check_args_parity(
    entry: ModelSkillTreeEntry,
) -> list[ModelSkillContractViolation]:
    if not entry.has_skill_md:
        return []

    spec_args = _frontmatter_flags(
        _parse_frontmatter(_text(entry.skill_md_text, "SKILL.md", entry))
    )
    if not spec_args:
        return []

    if not entry.has_prompt_md:
        return [
            _violation(
                entry,
                EnumSkillContractCheck.ARGS_PARITY,
                EnumSeverity.WARNING,
                f"SKILL.md declares {len(spec_args)} args but no prompt.md exists",
            )
        ]

    prompt_text = _text(entry.prompt_md_text, "prompt.md", entry)
    violations: list[ModelSkillContractViolation] = []
    for arg in spec_args:
        bare = arg.lstrip("-")
        if (
            arg not in prompt_text
            and bare not in prompt_text
            and bare.replace("-", "_") not in prompt_text
        ):
            violations.append(
                _violation(
                    entry,
                    EnumSkillContractCheck.ARGS_PARITY,
                    EnumSeverity.ERROR,
                    f"Arg '{arg}' declared in SKILL.md but not found in prompt.md",
                )
            )
    return violations


def _check_sub_skill_exists(
    entry: ModelSkillTreeEntry, tree: ModelSkillTreeSnapshot
) -> list[ModelSkillContractViolation]:
    if not entry.has_prompt_md:
        return []

    violations: list[ModelSkillContractViolation] = []
    for ref in _sub_skill_refs(_text(entry.prompt_md_text, "prompt.md", entry)):
        if _resolve_skill(tree, ref) is None:
            dir_name_underscore = ref.replace("-", "_")
            violations.append(
                _violation(
                    entry,
                    EnumSkillContractCheck.SUB_SKILL_EXISTS,
                    EnumSeverity.ERROR,
                    f"Sub-skill 'onex:{ref}' referenced but neither "
                    f"'{dir_name_underscore}/' nor '{ref}/' "
                    f"found under skills root",
                )
            )
    return violations


def _check_sub_skill_args(
    entry: ModelSkillTreeEntry, tree: ModelSkillTreeSnapshot
) -> list[ModelSkillContractViolation]:
    if not entry.has_prompt_md:
        return []

    violations: list[ModelSkillContractViolation] = []
    prompt_text = _text(entry.prompt_md_text, "prompt.md", entry)
    for skill_name, dispatched_flags in _sub_skill_dispatches(prompt_text):
        if not dispatched_flags:
            continue
        target = _resolve_skill(tree, skill_name)
        if target is None:
            # sub-skill-exists reports an unresolved target.
            continue

        declared: set[str] = set()
        target_text = _text(target.skill_md_text, "SKILL.md", target)
        for arg in _frontmatter_flags(_parse_frontmatter(target_text)):
            declared.update((arg, arg.lstrip("-"), arg.lstrip("-").replace("-", "_")))

        for flag in dispatched_flags:
            bare = flag.lstrip("-")
            if (
                flag not in declared
                and bare not in declared
                and bare.replace("-", "_") not in declared
            ):
                violations.append(
                    _violation(
                        entry,
                        EnumSkillContractCheck.SUB_SKILL_ARGS,
                        EnumSeverity.WARNING,
                        f"Dispatched arg '{flag}' to 'onex:{skill_name}' "
                        f"but it is not declared in target's SKILL.md args",
                    )
                )
    return violations


def _check_duplicate_frontmatter(
    entry: ModelSkillTreeEntry,
) -> list[ModelSkillContractViolation]:
    if not entry.has_skill_md:
        return []

    violations: list[ModelSkillContractViolation] = []
    in_frontmatter = False
    seen_keys: dict[str, int] = {}
    lines = _text(entry.skill_md_text, "SKILL.md", entry).splitlines()
    for i, line in enumerate(lines, 1):
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if (
            in_frontmatter
            and ":" in line
            and not line.startswith(" ")
            and not line.startswith("\t")
        ):
            key = line.split(":", 1)[0].strip()
            if key in seen_keys:
                violations.append(
                    _violation(
                        entry,
                        EnumSkillContractCheck.DUPLICATE_FRONTMATTER,
                        EnumSeverity.ERROR,
                        f"Duplicate key '{key}' in frontmatter "
                        f"(lines {seen_keys[key]} and {i})",
                    )
                )
            else:
                seen_keys[key] = i
    return violations


def _check_spec_prompt_predicates(
    entry: ModelSkillTreeEntry,
) -> list[ModelSkillContractViolation]:
    if not entry.has_skill_md or not entry.has_prompt_md:
        return []

    body = _skill_body(_text(entry.skill_md_text, "SKILL.md", entry))
    prompt_text = _text(entry.prompt_md_text, "prompt.md", entry)
    violations: list[ModelSkillContractViolation] = []
    for line in body.splitlines():
        line_lower = line.lower()
        if not any(kw in line_lower for kw in _PREDICATE_CONTEXT_KEYWORDS):
            continue
        for pred in _RE_BACKTICK_PREDICATE.findall(line):
            if len(pred) < 3:
                continue
            if (
                pred not in prompt_text
                and pred.replace("_", "-") not in prompt_text
                and pred.replace("-", "_") not in prompt_text
            ):
                violations.append(
                    _violation(
                        entry,
                        EnumSkillContractCheck.SPEC_PROMPT_PREDICATES,
                        EnumSeverity.WARNING,
                        f"Predicate '{pred}' found in SKILL.md but not in prompt.md",
                    )
                )
    return violations


def _promote(
    violation: ModelSkillContractViolation, strict: bool
) -> ModelSkillContractViolation:
    if (
        strict
        and violation.check in _STRICT_PROMOTED
        and violation.severity == EnumSeverity.WARNING
    ):
        return violation.model_copy(update={"severity": EnumSeverity.ERROR})
    return violation


class HandlerSkillContractValidate:
    """Stateless, pure contract-parity verdict over the request's skills-tree snapshot."""

    def handle(
        self, request: ModelSkillContractValidateRequest
    ) -> ModelSkillContractValidateResult:
        tree = request.tree
        found: list[ModelSkillContractViolation] = []
        for entry in tree.entries:
            if not entry.is_dir or entry.name.startswith("_"):
                continue
            found.extend(_check_args_parity(entry))
            found.extend(_check_sub_skill_exists(entry, tree))
            found.extend(_check_sub_skill_args(entry, tree))
            found.extend(_check_duplicate_frontmatter(entry))
            found.extend(_check_spec_prompt_predicates(entry))

        violations = tuple(_promote(v, request.strict) for v in found)
        errors = sum(v.severity == EnumSeverity.ERROR for v in violations)
        return ModelSkillContractValidateResult(
            skills_root=tree.skills_root,
            strict=request.strict,
            violations=violations,
            error_count=errors,
            warning_count=sum(v.severity == EnumSeverity.WARNING for v in violations),
            passed=errors == 0,
        )


__all__ = ["HandlerSkillContractValidate"]
