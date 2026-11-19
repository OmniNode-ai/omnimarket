# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared interpretation of CLAIM cause cells."""

import re

_SPELLING = re.compile(r"[\s_-]+")


def canonical_check(name: str) -> str:
    """Case-fold a claim's check name and collapse spacing and separators."""
    return _SPELLING.sub(" ", str(name).casefold()).strip()


def cause_cells(text: str) -> list[tuple[str, str]]:
    """The ``cause=<repo>:<check>`` cells on a row's first line, as (repo, check)."""
    out = []
    for cell in (c.strip() for c in text.split("\n", 1)[0].split("|")):
        if cell.startswith("cause="):
            cause_repo, _, check = cell[len("cause=") :].strip().partition(":")
            out.append((cause_repo.strip(), check.strip()))
    return out
