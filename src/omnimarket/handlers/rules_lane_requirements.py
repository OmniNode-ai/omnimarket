# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Column-zero lane requirements shared by fill and placement recovery."""

import re


def declarations(brief: str) -> list[str]:
    """Only column-zero metadata outside fenced quotations declares an engine requirement."""
    out, fence = [], None
    for line in brief.splitlines():
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            fence = None if fence == marker else marker if fence is None else fence
        elif fence is None:
            out.append(line)
    return out


def claude_required(brief: str) -> bool:
    for line in declarations(brief):
        if re.fullmatch(
            r"Required (?:engine|model):\s*(?:claude(?:[-_](?:opus|sonnet))?|opus|sonnet)\s*",
            line,
            re.IGNORECASE,
        ):
            return True
        if line.startswith("ROUTE:") and re.search(
            r"\b(?:pinned|required)=true\b|\bfallback=none\b", line
        ):
            return True
    return False
