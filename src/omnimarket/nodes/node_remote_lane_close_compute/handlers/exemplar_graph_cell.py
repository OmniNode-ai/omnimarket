# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The exemplar and graph cells a code lane's TERMINAL carries (OMN-24686).

Operator 2026-11-16: lanes guess instead of copying. A lane whose brief tells it to find an exemplar and query the code graph
reports ``exemplar=<path>`` (or ``exemplar=none:<reason>``) and ``graph=<query -> answer>`` on its unfenced TERMINAL or
LANE_RESULT line (or as keys of a JSON report); ``graph=unreachable:<why>`` and ``exemplar=none:<reason>`` are accepted and
kept as findings, never as a silent pass. The closing decision marks a finishing outcome (done, handed-off) of a code lane that
carries neither ``partial`` with the problem ``no exemplar or graph cell``, the way ``no DELEGATION line in the final message``
rejects a lane with no delegation.

One definition of a code lane: the brief check (``lane_kind_rules.json`` beside the lane-brief skill and the lane placement
node's handler, in the private plugin repository) decides which briefs must carry an EXEMPLAR and a GRAPH step, with its own writes-code
words and its open-ended exemption. This node holds no word list of its own: a brief is a code lane here when it carries such a
step (a ``NAME: <text>`` line or a non-empty ``## NAME`` section of its own prose) and is not marked ``Lane kind: open-ended``.

The check proves reporting only: that the lane said which exemplar it copied and which graph query it ran, not that the
exemplar is the closest one or that the query was run.
"""

from __future__ import annotations

import json
import re

NO_CELL_PROBLEM = "no exemplar or graph cell"
FINISHING_OUTCOMES = frozenset({"done", "handed-off"})
UNREACHABLE_PREFIX = "unreachable:"
NONE_PREFIX = "none:"
STEP_CELLS = ("exemplar", "graph")
REPORT_END_RE = re.compile(r"^[^\S\r\n]*`?DELEGATION[^\S\r\n]+", re.M)

CLAIM_REPO_RE = re.compile(r"\|\s*CLAIM\s*\|[^\n]*?\|\s*repo=(?P<repo>[^\s|]+)")
NO_REPO = frozenset({"", "-", "none", "n/a", "na", "tbd", "<repo>"})
RULES_BLOCK_RE = re.compile(
    r"^# Standing rules for every dispatched lane[^\n]*\n.*?(?=^#{1,2} |\Z)",
    re.M | re.S,
)
CONSTRAINTS_BLOCK_RE = re.compile(
    r"^## 3a\. Constraints[^\n]*\n.*?(?=^## |\Z)", re.M | re.S
)
LANE_KIND_RE = re.compile(r"^[ \t]*Lane kind:[ \t]*(open-ended|ticket)[ \t]*$", re.M)
STEP_NAMES = ("EXEMPLAR", "GRAPH")
# A section runs from its heading to the next heading of any level.
HEADING_RE = re.compile(
    r"^#{1,6}[ \t]+(?:\d+\.[ \t]*)?\**(?P<name>[A-Za-z]+)\**[ \t:]*(?P<rest>[^\n]*)$",
    re.M,
)

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


def carries_step(prose: str, name: str) -> bool:
    """True when the prose carries a real ``NAME: <text>`` line or a ``## NAME`` section with a real body."""
    for found in re.finditer(
        rf"^[ \t]*(?:[-*][ \t]+)?\**{name}:\**[ \t]*(\S.*?)[ \t]*$", prose, re.M | re.I
    ):
        if real_step_text(found.group(1)):
            return True
    headings = list(HEADING_RE.finditer(prose))
    for index, heading in enumerate(headings):
        if heading.group("name").upper() != name:
            continue
        stop = headings[index + 1].start() if index + 1 < len(headings) else len(prose)
        body = (heading.group("rest") + "\n" + prose[heading.end() : stop]).strip()
        if real_step_text(body):
            return True
    return False


def real_step_text(text: str) -> bool:
    """A step's text is no template and no placeholder."""
    bare = (
        re.sub(r"^none\s*:", "", text.strip(), flags=re.I).strip().rstrip(".!").strip()
    )
    return (
        bool(bare) and not TEMPLATE_RE.search(text) and bare.lower() not in PLACEHOLDERS
    )


def code_lane(brief: str) -> bool:
    """True when the brief's CLAIM names a repo, it is not open-ended and its own prose carries an EXEMPLAR or GRAPH step."""
    claim = CLAIM_REPO_RE.search(brief or "")
    if claim is None or claim.group("repo").strip().lower() in NO_REPO:
        return False
    prose = task_prose(brief)
    kind = LANE_KIND_RE.search(prose)
    if kind is not None and kind.group(1) == "open-ended":
        return False
    return any(carries_step(prose, name) for name in STEP_NAMES)


def report_part(final_message: str | None) -> str:
    """The final report: everything before the DELEGATION line, as the outcome reader takes it."""
    return REPORT_END_RE.split(final_message or "", maxsplit=1)[0].strip()


def declaration_cells(final_message: str | None) -> dict[str, str]:
    """The exemplar and graph cells of the report: its unfenced TERMINAL and LANE_RESULT lines (the first of each key wins),
    or the keys of a JSON report. Lines after the DELEGATION line are not read."""
    report = report_part(final_message)
    try:
        data = json.loads(report)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        return {
            key: value.strip()
            for key in STEP_CELLS
            if isinstance(value := data.get(key), str)
        }
    cells: dict[str, str] = {}
    fence: str | None = None
    for raw in report.splitlines():
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


def real_exemplar(value: str) -> str | None:
    """``path`` for a path to copy, ``none:<reason>`` for a stated absence with a reason, else None (a template, a placeholder
    or a bare word)."""
    if TEMPLATE_RE.search(value):
        return None
    if value.lower().startswith(NONE_PREFIX):
        why = value[len(NONE_PREFIX) :].strip()
        return None if why.lower() in PLACEHOLDERS else f"none:{why}"
    if value.lower() in PLACEHOLDERS or not re.search(r"[/.]", value):
        return None
    return "path"


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


def ledger_cell(value: str) -> str:
    """A cell value as one ledger cell: one line, no ``|``, at most 200 characters."""
    return " ".join(value.replace("|", "/").split())[:200]


def exemplar_graph_cells(
    final_message: str | None,
) -> tuple[bool, tuple[str, ...], dict[str, str]]:
    """``(carried, findings, cells)``: whether both cells are real, the findings an accepted ``exemplar=none:<reason>`` or
    unreachable graph is recorded as, and the two cells as ledger cells (empty unless both are real)."""
    cells = declaration_cells(final_message)
    exemplar_value, graph_value = cells.get("exemplar", ""), cells.get("graph", "")
    exemplar, reading = real_exemplar(exemplar_value), graph_reading(graph_value)
    if exemplar is None or reading is None:
        return False, (), {}
    findings: list[str] = []
    if exemplar.startswith(NONE_PREFIX):
        findings.append(f"exemplar none: {exemplar[len(NONE_PREFIX) :]}")
    if reading.startswith(UNREACHABLE_PREFIX):
        findings.append(f"graph unreachable: {reading[len(UNREACHABLE_PREFIX) :]}")
    return (
        True,
        tuple(findings),
        {
            "exemplar": ledger_cell(exemplar_value),
            "graph": ledger_cell(graph_value),
        },
    )
