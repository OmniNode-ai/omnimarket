# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The undeclared read check: the reads a diff adds, judged against the settings the overlays declare.

Operator direction 2026-11-21: every setting a lane, host, CI job or image build reads is declared in
overlays, and a check fails on an undeclared read. This handler is that check's decision; a CI job and a
pre-commit hook feed it a unified diff and the overlay documents.

Only ADDED lines in the code of the repository count (``src/``, ``scripts/``, ``docker/``, workflow files
and Dockerfiles), so an old read is never refused for being old, and a test that stubs a variable is
outside the scope. Three kinds of read are found: an environment variable read in Python or shell, a
repository variable of a workflow expression, and an image build argument.

A name is declared when it is a whole string value of any overlay document (the way overlays name the
variables a node reads) or a line ``<scope> NAME=value`` of the ``declared_settings`` register, or it begins
with a prefix the register lists under ``platform_env_prefixes`` (the runner's own variables).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from omnimarket.nodes.node_undeclared_read_check_compute.models import (
    EnumReadKind,
    ModelRead,
    ModelUndeclaredReadCheckRequest,
    ModelUndeclaredReadCheckResult,
)

REGISTER = "declared_settings"
REGISTER_LISTS = (
    "declared_env",
    "declared_repo_variables",
    "declared_build_args",
    "declared_broker_settings",
)
PREFIX_LIST = "platform_env_prefixes"
SCOPE_PREFIXES = ("src/", "scripts/", "docker/", ".github/workflows/")
OUT_OF_SCOPE_SEGMENTS = ("tests/", "fixtures/", "test_")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_PY_ENV = re.compile(
    rf"""(?:\bos\.)?\benviron(?:\[|\.get\()\s*["']({_NAME})["']"""
    rf"""|\b(?:os\.)?getenv\(\s*["']({_NAME})["']"""
)
_SHELL_ENV = re.compile(rf"\$\{{({_NAME})(?::?[-=?+])")
_WORKFLOW_VAR = re.compile(rf"\$\{{\{{\s*vars\.({_NAME})")
_BUILD_ARG = re.compile(rf"^\s*ARG\s+({_NAME})")
_ENTRY = re.compile(rf"^\S+ ({_NAME})=")
_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{1,}$")


def _in_scope(path: str) -> bool:
    if not path.startswith(SCOPE_PREFIXES) and not path.rsplit("/", 1)[-1].startswith(
        "Dockerfile"
    ):
        return False
    return not any(segment in path for segment in OUT_OF_SCOPE_SEGMENTS)


def added_lines(diff_text: str) -> Iterator[tuple[str, int, str]]:
    """Each added line of a unified diff as (path, line number in the new file, text)."""
    path: str | None = None
    number = 0
    in_header = True
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            in_header = True
            path = None
            continue
        if in_header:
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                path = None if target == "/dev/null" else target.removeprefix("b/")
            elif raw.startswith("@@"):
                in_header = False
            else:
                continue
        hunk = _HUNK.match(raw)
        if hunk:
            number = int(hunk.group(1))
            continue
        if path is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            yield path, number, raw[1:]
            number += 1
        elif raw.startswith(" "):
            number += 1


def _reads_of(path: str, text: str) -> list[tuple[str, EnumReadKind]]:
    stripped = text.lstrip()
    found: list[tuple[str, EnumReadKind]] = []
    base = path.rsplit("/", 1)[-1]
    if path.endswith(".py") and not stripped.startswith("#"):
        for match in _PY_ENV.finditer(text):
            found.append((match.group(1) or match.group(2), EnumReadKind.ENV))
    elif path.endswith(".sh") and not stripped.startswith("#"):
        found += [(m.group(1), EnumReadKind.ENV) for m in _SHELL_ENV.finditer(text)]
    elif path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        found += [
            (m.group(1), EnumReadKind.REPO_VARIABLE)
            for m in _WORKFLOW_VAR.finditer(text)
        ]
    if base.startswith("Dockerfile"):
        arg = _BUILD_ARG.match(text)
        if arg:
            found.append((arg.group(1), EnumReadKind.BUILD_ARG))
    return found


def _string_values(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _string_values(value)


def declared_names(documents: dict[str, Any]) -> tuple[set[str], tuple[str, ...]]:
    """The declared names, and the platform prefixes the register lists."""
    names: set[str] = set()
    for document in documents.values():
        names.update(value for value in _string_values(document) if _TOKEN.match(value))
    register = documents.get(REGISTER)
    prefixes: tuple[str, ...] = ()
    if isinstance(register, dict):
        for key in REGISTER_LISTS:
            for line in register.get(key) or []:
                match = _ENTRY.match(line) if isinstance(line, str) else None
                if match:
                    names.add(match.group(1))
        prefixes = tuple(
            p for p in register.get(PREFIX_LIST) or [] if isinstance(p, str)
        )
    return names, prefixes


class HandlerUndeclaredReadCheck:
    def handle(
        self, request: ModelUndeclaredReadCheckRequest
    ) -> ModelUndeclaredReadCheckResult:
        reads: list[ModelRead] = []
        for path, number, text in added_lines(request.diff_text):
            if not _in_scope(path):
                continue
            reads.extend(
                ModelRead(
                    path=path, line=number, name=name, kind=kind, text=text.strip()
                )
                for name, kind in _reads_of(path, text)
            )
        names, prefixes = declared_names(request.overlay_documents)
        undeclared = tuple(
            read
            for read in reads
            if read.name not in names and not read.name.startswith(prefixes)
        )
        return ModelUndeclaredReadCheckResult(
            reads=tuple(reads),
            undeclared=undeclared,
            register_present=isinstance(request.overlay_documents.get(REGISTER), dict),
        )


__all__ = ["HandlerUndeclaredReadCheck", "added_lines", "declared_names"]
