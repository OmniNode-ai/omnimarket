# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Portable path rendering for worktree report delivery."""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from ..models import ModelReportRoot


def _replacement(value: str, _match: re.Match[str]) -> str:
    return value


INSTALLED_LIB = ".omninode/morning-workflows/lib"


def portabilize(
    text: str, roots: Sequence[ModelReportRoot], home: Path | None = None
) -> str:
    """Rewrite known roots, including root-qualified tokens in copied CLAIMs.

    Reports can quote a CLAIM from a different host, with a relative registry
    prefix or a home-relative prefix. Recognize those prefixes by the supplied
    root's directory name. Unknown paths remain for the document scrub to refuse.
    Longest roots win, and token boundaries keep sibling paths and URLs intact.
    """
    pairs: list[tuple[str, str]] = []
    aliases: list[tuple[str, str]] = []
    for index, root in enumerate(roots):
        prefix = "" if index == 0 else f"{root.label}/"
        pairs.extend(
            [
                (str(root.worktrees).rstrip("/"), f"{prefix}omni_worktrees"),
                (str(root.registry).rstrip("/"), prefix.rstrip("/") or "."),
            ]
        )
        aliases.append((root.registry.name, prefix))
    if home is not None:
        pairs.append((str(home / INSTALLED_LIB), "."))
    for old, new in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        pattern = re.compile(
            r"(?<![\w/.:~-])" + re.escape(old) + r"(?=/|[\s\"'`|,}]|$)"
        )
        text = pattern.sub(partial(_replacement, new), text)
    text = re.sub(r"(?<![\w/.:~-])\./", "", text)
    for name, prefix in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
        # A copied CLAIM is the evidence that this foreign-host path names a
        # registry worktree. Do not infer ownership of arbitrary foreign paths
        # merely because one directory happens to have the registry's name.
        copied_claim = re.compile(
            r"(?<=worktree=)/(?:[^/\s\"'`]+/)*"
            + re.escape(name)
            + r"/(?=omni_worktrees/)"
        )
        text = copied_claim.sub(partial(_replacement, prefix), text)
        pattern = re.compile(r"(?<![\w/.:~-])(?:~/(?:Code/)?)?" + re.escape(name) + "/")
        text = pattern.sub(partial(_replacement, prefix), text)
    if roots:
        text = re.sub(r"\$\{OMNI_HOME\}/|\$OMNI_HOME/", "", text)
    return text


def scrub_refusal(output: str) -> str:
    """Keep refusal blocks visible when a record-class notice precedes them."""
    blocks: list[list[str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line[0].isspace() and blocks:
            blocks[-1].append(line.strip())
        else:
            blocks.append([line.strip()])
    kept = [
        b
        for b in blocks
        if not b[0].startswith("scrub_shared_doc: person-name scan SKIPPED")
    ]
    return " ".join(" ".join(b) for b in kept) or output.strip()
