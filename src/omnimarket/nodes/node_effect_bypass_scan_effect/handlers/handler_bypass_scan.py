# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBypassScan: count the lines of one checkout that reach the outside world outside an effect node (OMN-24432).

Follows node_join_refusal_effect: the effect's listing of files is injected (``ProtocolTrackedFiles``), the policy comes from the
contract or an overlay, and ``handle(request) -> result`` is the one public method. Reads files only; writes nothing.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Protocol

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models import (
    EnumBypassCategory,
    ModelBypassCounts,
    ModelBypassInventory,
    ModelBypassSite,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.models import (
    ModelBypassScanCommand,
    ModelBypassScanPolicy,
)


class ProtocolTrackedFiles(Protocol):
    """The files of a checkout that belong to the repository, as repo-relative posix paths."""

    def list_files(self, root: Path) -> tuple[str, ...]: ...


class GitTrackedFiles:
    """The files git tracks in a checkout. A local read: it talks to no remote."""

    def list_files(self, root: Path) -> tuple[str, ...]:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8", errors="replace")
        return tuple(p for p in out.split("\0") if p)


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """``**/`` is zero or more directories, ``**`` anything, ``*`` within one segment, ``?`` one character."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _docstring_lines(text: str) -> frozenset[int]:
    """Line numbers inside a standalone string expression (a docstring or a bare string); empty when the file does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno is not None
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return frozenset(lines)


class HandlerBypassScan:
    """Scan one checkout against one policy."""

    def __init__(
        self, policy: ModelBypassScanPolicy, file_lister: ProtocolTrackedFiles
    ) -> None:
        self._policy = policy
        self._lister = file_lister
        self._excludes = tuple(glob_to_regex(g) for g in policy.exclude_globs)
        self._sanctioned = {
            category: tuple(glob_to_regex(g) for g in globs)
            for category, globs in policy.sanctioned.items()
        }
        self._patterns = {
            category: tuple(re.compile(p) for p in patterns)
            for category, patterns in policy.categories.items()
        }

    def handle(self, request: ModelBypassScanCommand) -> ModelBypassInventory:
        root = Path(request.root)
        sites: list[ModelBypassSite] = []
        scanned = 0
        for rel in self._lister.list_files(root):
            if PurePosixPath(rel).suffix not in self._policy.suffixes:
                continue
            if any(rx.match(rel) for rx in self._excludes):
                continue
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            sites.extend(self._sites_of(rel, text))
        sites.sort(key=lambda s: (s.path, s.line, s.category.value))
        counts = {
            category: sum(1 for s in sites if s.category is category)
            for category in EnumBypassCategory
        }
        return ModelBypassInventory(
            repository=request.repository,
            files_scanned=scanned,
            counts=ModelBypassCounts(counts=counts),
            sites=tuple(sites),
        )

    def _sites_of(self, rel: str, text: str) -> list[ModelBypassSite]:
        skip = _docstring_lines(text) if rel.endswith(".py") else frozenset()
        found: list[ModelBypassSite] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if number in skip or line.lstrip().startswith(
                tuple(self._policy.comment_prefixes)
            ):
                continue
            for category, patterns in self._patterns.items():
                if any(rx.match(rel) for rx in self._sanctioned.get(category, ())):
                    continue
                if any(p.search(line) for p in patterns):
                    found.append(
                        ModelBypassSite(path=rel, line=number, category=category)
                    )
        return found
