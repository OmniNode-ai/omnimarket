# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The exemplar and graph cells a code lane's TERMINAL carries (OMN-24686).

Operator 2026-11-16: lanes guess instead of copying. A lane whose brief names a repo in its CLAIM and whose task writes code
copies an existing exemplar and finds importers and definitions in the code graph before it falls back to grep. Its closing
report therefore carries ``exemplar=<path>`` and ``graph=<query -> answer>`` on its unfenced TERMINAL or LANE_RESULT line;
``graph=unreachable:<why>`` is accepted and kept as a finding, never as a silent pass. The closing decision marks a finishing
outcome (done, handed-off) of a code lane that carries neither ``partial`` with the problem ``no exemplar or graph cell``,
the way ``no DELEGATION line in the final message`` rejects a lane with no delegation. A lane that writes no code, a brief that
names no repo and a request with no brief are not judged.
"""

from __future__ import annotations

import re

NO_CELL_PROBLEM = "no exemplar or graph cell"
FINISHING_OUTCOMES = frozenset({"done", "handed-off"})
UNREACHABLE_PREFIX = "unreachable:"

CLAIM_REPO_RE = re.compile(r"\|\s*CLAIM\s*\|[^\n]*?\|\s*repo=(?P<repo>[^\s|]+)")
NO_REPO = frozenset({"", "-", "none", "n/a", "na", "tbd", "<repo>"})
RULES_BLOCK_RE = re.compile(
    r"^# Standing rules for every dispatched lane[^\n]*\n.*?(?=^#{1,2} |\Z)",
    re.M | re.S,
)
CONSTRAINTS_BLOCK_RE = re.compile(
    r"^## 3a\. Constraints[^\n]*\n.*?(?=^## |\Z)", re.M | re.S
)
WRITE_VERB = (
    r"(?:add(?:s|ed|ing)?|writ(?:e|es|ing|ten)|implement(?:s|ed|ing)?|fix(?:es|ed|ing)?|chang(?:e|es|ed|ing)"
    r"|patch(?:es|ed|ing)?|refactor(?:s|ed|ing)?|port(?:s|ed|ing)?|build(?:s|ing)?|creat(?:e|es|ed|ing)"
    r"|edit(?:s|ed|ing)?|modif(?:y|ies|ied|ying)|replac(?:e|es|ed|ing)|wir(?:e|es|ed|ing)|open(?:s|ed|ing)?"
    r"|push(?:es|ed|ing)?)"
)
CODE_OBJECT = (
    r"(?:code|tests?|handlers?|nodes?|scripts?|functions?|modules?|contracts?|skills?|hooks?|pull requests?"
    r"|PRs?|patch(?:es)?|diffs?|bugs?|regressions?|logic)"
)
WRITES_CODE_RE = re.compile(
    rf"(?<![\w-]){WRITE_VERB}(?![\w-])[^.\n;]{{0,80}}?(?<![\w-]){CODE_OBJECT}(?![\w-])",
    re.I,
)
NEGATION_RE = re.compile(
    r"\b(?:no|not|never|nothing|none|without|don't|dont|cannot|can't|nor|read-only)\b",
    re.I,
)
CLAUSE_RE = re.compile(r"(?<=[.!?])\s+|\s*;\s*|\n")

CELL_KEYS = r"outcome|needs|withdrawn-by|exemplar|graph|friction|band|worktree|delegated|runs|codex|jev|lab|glm"
CELL_RE = re.compile(
    rf"(?:^|[\s|])(?P<key>exemplar|graph)=(?P<value>.*?)(?=\s+\|\s|\s+(?:{CELL_KEYS})=|$)"
)
DECLARATION_RE = re.compile(r"^(?:LANE_RESULT|TERMINAL)(?:\s|$)")
FENCE_MARKERS = ("```", "~~~")
TEMPLATE_RE = re.compile(r"<[^<>]*>")
PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        ".",
        "?",
        "n/a",
        "na",
        "nil",
        "none",
        "nothing",
        "tbd",
        "todo",
        "unknown",
    }
)


def task_prose(brief: str) -> str:
    """The brief's own prose: no fenced example, pasted rules block, generated 3a block or quoted ticket text."""
    kept: list[str] = []
    fence: str | None = None
    for line in brief.splitlines():
        marker = line.lstrip()[:3]
        if marker in FENCE_MARKERS:
            fence = None if fence == marker else marker if fence is None else fence
        elif fence is None and not line.lstrip().startswith(">"):
            kept.append(line)
    text = RULES_BLOCK_RE.sub("", "\n".join(kept))
    return CONSTRAINTS_BLOCK_RE.sub("", text)


def writes_code(prose: str) -> bool:
    """True when a clause of the prose writes code and is not negated ("writes no code", "do not add a handler")."""
    for clause in CLAUSE_RE.split(prose):
        found = WRITES_CODE_RE.search(clause)
        if found and not NEGATION_RE.search(clause[: found.end()]):
            return True
    return False


def code_lane(brief: str) -> bool:
    """True when the brief's CLAIM names a repo and its task writes code."""
    claim = CLAIM_REPO_RE.search(brief or "")
    if claim is None or claim.group("repo").strip().lower() in NO_REPO:
        return False
    return writes_code(task_prose(brief))


def declaration_cells(final_message: str | None) -> dict[str, str]:
    """The exemplar and graph cells of the unfenced TERMINAL and LANE_RESULT lines; the first of each key wins."""
    cells: dict[str, str] = {}
    fence: str | None = None
    for raw in (final_message or "").splitlines():
        line = raw.strip()
        if line[:3] in FENCE_MARKERS:
            fence = None if fence == line[:3] else line[:3] if fence is None else fence
        elif fence is None and DECLARATION_RE.match(line):
            for found in CELL_RE.finditer(line):
                cells.setdefault(
                    found.group("key"),
                    found.group("value").strip().strip("`").rstrip(".,;").strip(),
                )
    return cells


def real_exemplar(value: str) -> bool:
    """A path to copy: not a template, a placeholder or a bare word."""
    return (
        not TEMPLATE_RE.search(value)
        and value.lower() not in PLACEHOLDERS
        and bool(re.search(r"[/.]", value))
    )


def graph_reading(value: str) -> str | None:
    """``unreachable:<why>`` when the cell records an unreachable graph with a reason, ``answered`` for ``<query> -> <answer>``, else None."""
    if value.lower().startswith(UNREACHABLE_PREFIX):
        why = value[len(UNREACHABLE_PREFIX) :].strip()
        return (
            None
            if TEMPLATE_RE.search(why) or why.lower() in PLACEHOLDERS
            else f"unreachable:{why}"
        )
    query, arrow, answer = value.partition("->")
    if (
        arrow
        and query.strip().lower() not in PLACEHOLDERS
        and answer.strip().lower() not in PLACEHOLDERS
    ):
        return None if TEMPLATE_RE.search(value) else "answered"
    return None


def exemplar_graph_cells(final_message: str | None) -> tuple[bool, tuple[str, ...]]:
    """``(carried, findings)``: whether both cells are real, and the finding an accepted unreachable graph is recorded as."""
    cells = declaration_cells(final_message)
    reading = graph_reading(cells.get("graph", ""))
    if reading is None or not real_exemplar(cells.get("exemplar", "")):
        return False, ()
    if reading.startswith(UNREACHABLE_PREFIX):
        return True, (f"graph unreachable: {reading[len(UNREACHABLE_PREFIX) :]}",)
    return True, ()
