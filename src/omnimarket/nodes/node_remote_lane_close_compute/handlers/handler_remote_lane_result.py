# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a finished remote lane closes (OMN-20669).

A port of the remote-lane runner's ``closing_outcome`` and the readers under it. An
engine failure stays ``failed``; a clean exit whose last DELEGATION line does not
close the lane is ``rejected-no-delegation``; otherwise the outcome is the one the
final report declares on a ``LANE_RESULT`` or ``TERMINAL`` line (or in a JSON report),
and anything missing, quoted inside a fence or contradictory is ``unknown``. A clean
exit alone never proves the work is done.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import (
    FAILED_OUTCOME,
    LANE_OUTCOMES,
    REJECTED_OUTCOME,
    ModelRemoteLaneResult,
    ModelRemoteLaneResultRequest,
)

DELEGATION_LINE_RE = re.compile(r"^[^\S\r\n]*`?DELEGATION[^\S\r\n]+(.*)$", re.M)
DELEGATION_REASON_RE = re.compile(
    r"no-text-or-code|read-only-lane|route-refused:[A-Za-z0-9._-]+|route-unavailable:[A-Za-z0-9._:-]+"
)
DELEGATION_KEYS = frozenset(
    {"delegated", "runs", "codex", "lab", "glm", "jev", "reason", "delegation_reason"}
)
DECLARATION_RE = re.compile(r"^(?:LANE_RESULT|TERMINAL)(?:\s|$)")
OUTCOME_CELL_RE = re.compile(r"(?:^|[\s|])outcome=([^\s|]+)")
# A final message that says the lane is waiting on a background task or a notification.
WAITING_RE = re.compile(
    r"background|(?:i['\u2019]ll|i will|will|to) be notified|notified (?:when|once)|notification"
    r"|waiting (?:for|on) (?:the |a |ci|it|its|them|that|this)",
    re.I,
)
HANDOFF_ID_RE = re.compile(
    r"\bmsg=\S|\bhanded-off=|\bhandoff id\b|\bid=[0-9A-Za-z]|\bHanded \S+#\d+", re.I
)


def parse_delegation_line(final_message: str | None) -> dict[str, str]:
    """The key=value cells of the last DELEGATION line; extra cells and prose are ignored."""
    found = DELEGATION_LINE_RE.findall(final_message or "")
    cells: dict[str, str] = {}
    for token in found[-1].split() if found else []:
        key, sep, value = token.partition("=")
        if sep and key in DELEGATION_KEYS:
            name = "reason" if key == "delegation_reason" else key
            cells[name] = value.strip("`").rstrip("`,;.")
    return cells


def delegation_problem(
    final_message: str | None, reasons: tuple[str, ...]
) -> str | None:
    """Why the last DELEGATION line does not close the lane, or None when it does."""
    cells = parse_delegation_line(final_message)
    try:
        count = int(cells.get("delegated", ""))
    except ValueError:
        return "no DELEGATION line in the final message"
    if count > 0:
        if any(
            cells.get(key, "") not in {"", "none"} for key in ("runs", "codex", "jev")
        ):
            return None
        return f"DELEGATION delegated={count} names no runs= (or codex=/jev=) ids"
    reason = cells.get("reason", "")
    if DELEGATION_REASON_RE.fullmatch(reason):
        return None
    return (
        f"DELEGATION delegated=0 reason={reason[:60] or 'none'} is not one of "
        + ", ".join(reasons)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate result key")
        obj[key] = value
    return obj


def _declared(value: object) -> str:
    return value if isinstance(value, str) and value in LANE_OUTCOMES else "unknown"


def final_lane_outcome(final_message: str | None) -> str:
    """The outcome the final report declares; prose and quoted examples declare none."""
    report = DELEGATION_LINE_RE.split(final_message or "", maxsplit=1)[0].strip()
    try:
        data = json.loads(report, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        return _declared(data.get("outcome"))
    declarations: list[str] = []
    fence: str | None = None
    for raw in report.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        if DECLARATION_RE.match(line):
            values = OUTCOME_CELL_RE.findall(line)
            value = values[0].rstrip(".,;") if len(values) == 1 else "unknown"
            declarations.append(_declared(value))
        elif line.startswith("{"):
            try:
                obj = json.loads(line, object_pairs_hook=_unique_object)
            except (ValueError, TypeError):
                declarations.append("unknown")
                continue
            declarations.append(
                _declared(obj.get("outcome") if isinstance(obj, dict) else None)
            )
    distinct = set(declarations)
    return distinct.pop() if len(distinct) == 1 else "unknown"


def waiting_on_background(final_message: str | None) -> bool:
    """True when the final text reads as waiting on a background task and names no handoff."""
    text = final_message or ""
    return bool(WAITING_RE.search(text)) and not HANDOFF_ID_RE.search(text)


class HandlerRemoteLaneResult:
    """Decide how a finished remote lane closes from its exit code and final message."""

    def handle(self, request: ModelRemoteLaneResultRequest) -> ModelRemoteLaneResult:
        message = request.final_message
        problem = delegation_problem(message, request.delegation_reasons)
        declared = final_lane_outcome(message)
        if request.engine_exit_code != 0:
            outcome = FAILED_OUTCOME
        elif problem is not None:
            outcome = REJECTED_OUTCOME
        else:
            outcome = declared
        return ModelRemoteLaneResult(
            outcome=outcome,
            problem=problem,
            declared_outcome=declared,
            delegation=parse_delegation_line(message),
            waiting_on_background=waiting_on_background(message),
        )
