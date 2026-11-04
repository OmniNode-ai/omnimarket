# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive exact writable files and declared checks from a delegated code task.

The handler is pure: callers supply tracked paths and an interpreter. A task
naming no tracked file yields no scope, so the write scope is never guessed."""

from __future__ import annotations

import re
import shlex

from .models import ModelCodeEditScopeRequest, ModelCodeEditScopeResult

MAX_NAMED_FILES = 20
_TOKEN = re.compile(r"[\w@+./-]+")
_GLOB = frozenset("*?[]{}")


def named_files(task: str, tracked: frozenset[str]) -> list[str]:
    """The tracked files the task text names, in first-mention order, at most MAX_NAMED_FILES."""
    found: list[str] = []
    for token in _TOKEN.findall(task):
        path = token.removeprefix("./").rstrip(".,:;")
        if path in tracked and path not in found and not _GLOB & set(path):
            found.append(path)
    return found[:MAX_NAMED_FILES]


class HandlerCodeEditScope:
    def handle(self, request: ModelCodeEditScopeRequest) -> ModelCodeEditScopeResult:
        files = named_files(request.task, frozenset(request.tracked_files))
        if not files:
            return ModelCodeEditScopeResult()
        checks = ["diff=git diff --check"]
        if python := [f for f in files if f.endswith(".py")]:
            checks.append(
                "syntax=" + shlex.join([request.python, "-m", "py_compile", *python])
            )
        return ModelCodeEditScopeResult(writable=files, checks=checks)
