# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One typed condition of a task-kind rule over a caller-scope field (plan section 4.4)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_context_task_kind_operator import (
    EnumContextTaskKindOperator,
)

#: Caller-scope fields a condition may read (core ``ModelContextCallerScope``).
TypeContextScopeField = Literal[
    "task_class",
    "node_archetype",
    "request_paths",
    "repository",
    "lane",
    "lane_kind",
    "cited_ruling_stamps",
    "cited_plan_paths",
    "ticket",
]
#: Caller-scope fields holding paths; only these accept the ``glob`` operator.
PATH_SCOPE_FIELDS: frozenset[str] = frozenset({"request_paths", "cited_plan_paths"})
#: Glob syntax this port does not support; a pattern using it is malformed, never literal.
_UNSUPPORTED_GLOB_CHARS = frozenset("[]{}\\!")


def compile_path_glob(pattern: str) -> re.Pattern[str]:
    """Compile a path glob to an anchored regex (port item AP-S8).

    ``*`` and ``?`` never cross ``/``; a whole ``**`` segment matches zero or more
    directories. The match is over the full path, so ``*.py`` does not match
    ``services/app.py`` while ``services/**/*.py`` does. A malformed pattern raises
    ``ValueError``: it is never dropped and never matched literally.
    """
    if not pattern:
        raise ValueError("path glob is empty")
    if pattern.startswith("/") or pattern.endswith("/"):
        raise ValueError(f"path glob {pattern!r} has a leading or trailing '/'")
    bad = sorted(set(pattern) & _UNSUPPORTED_GLOB_CHARS)
    if bad:
        raise ValueError(f"path glob {pattern!r} uses unsupported syntax {bad}")
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "":
            raise ValueError(f"path glob {pattern!r} has an empty segment")
        if segment == "**":
            parts.append(".*" if last else "(?:.*/)?")
            continue
        if "**" in segment:
            raise ValueError(
                f"path glob {pattern!r}: '**' must be a whole segment, got {segment!r}"
            )
        body = "".join(
            "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
            for char in segment
        )
        parts.append(body if last else body + "/")
    return re.compile("".join(parts))


def path_glob_matches(pattern: str, path: str) -> bool:
    """True when ``path`` matches ``pattern`` over its full length."""
    return compile_path_glob(pattern).fullmatch(path) is not None


class ModelContextTaskKindCondition(BaseModel):
    """``scope_field`` ``operator`` ``value``, optionally negated.

    A descriptor the caller does not supply is absent and fails the condition
    whether or not it is negated: an absent fact is never read as a mismatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_field: TypeContextScopeField
    operator: EnumContextTaskKindOperator
    value: str = Field(min_length=1)
    negate: bool = False

    @model_validator(mode="after")
    def _glob_only_on_paths(self) -> ModelContextTaskKindCondition:
        if self.operator is EnumContextTaskKindOperator.GLOB:
            if self.scope_field not in PATH_SCOPE_FIELDS:
                raise ValueError(
                    f"operator glob applies only to {sorted(PATH_SCOPE_FIELDS)}, "
                    f"not {self.scope_field}"
                )
            compile_path_glob(self.value)
        return self


__all__ = [
    "PATH_SCOPE_FIELDS",
    "ModelContextTaskKindCondition",
    "TypeContextScopeField",
    "compile_path_glob",
    "path_glob_matches",
]
