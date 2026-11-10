# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a finished lane asks for next, and its report text, for the closing row (OMN-23417).

The lane declares ``needs=<action>:<target>`` on an unfenced LANE_RESULT or TERMINAL line (or as the ``needs`` key of a JSON
report). A cell that is missing where the policy requires one, malformed, outside the policy's actions or contradictory is
kept as ``unparsed`` with its raw text; the close is never refused for it, so no finding is dropped. The report text is the
final message without its DELEGATION line, as one line with no cell separator, cut to the policy's bound.
"""

from __future__ import annotations

import json
import re

from omnimarket.models.remote_lane_close import (
    EnumLaneNeedsAction,
    ModelRemoteLaneNeeds,
    ModelRemoteLaneNeedsPolicy,
)

_DELEGATION_LINE_RE = re.compile(r"^[^\S\r\n]*`?DELEGATION[^\S\r\n]+", re.M)
_DECLARATION_RE = re.compile(r"^(?:LANE_RESULT|TERMINAL)(?:\s|$)")
_NEEDS_CELL_RE = re.compile(r"(?:^|[\s|])needs=([^\s|]+)")
_RAW_CHARS = 200


def _report(final_message: str | None) -> str:
    return _DELEGATION_LINE_RE.split(final_message or "", maxsplit=1)[0].strip()


def _json_needs(text: str) -> str | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    cell = data.get("needs") if isinstance(data, dict) else None
    return cell if isinstance(cell, str) and cell else None


def declared_needs(final_message: str | None) -> tuple[str, ...]:
    """Every needs value the unfenced declarations of the report carry, in order."""
    report = _report(final_message)
    whole = _json_needs(report)
    if whole is not None:
        return (whole,)
    found: list[str] = []
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
        if _DECLARATION_RE.match(line):
            found.extend(value.rstrip(".,;") for value in _NEEDS_CELL_RE.findall(line))
        elif line.startswith("{"):
            cell = _json_needs(line)
            if cell is not None:
                found.append(cell)
    return tuple(found)


def lane_needs(
    final_message: str | None, outcome: str, policy: ModelRemoteLaneNeedsPolicy
) -> ModelRemoteLaneNeeds | None:
    """The typed ask of the closing row, ``unparsed`` when it cannot be read, None when the lane owes none."""
    declared = declared_needs(final_message)
    if not declared:
        if outcome in policy.cell_required_outcomes:
            return ModelRemoteLaneNeeds(action=None, target="", parsed=False, raw="")
        return None
    first = declared[0]
    unparsed = ModelRemoteLaneNeeds(
        action=None, target="", parsed=False, raw=first[:_RAW_CHARS]
    )
    if len(set(declared)) > 1:
        return unparsed
    head, sep, target = first.partition(":")
    allowed = {action.value for action in policy.actions}
    if (
        not sep
        or head not in allowed
        or not re.fullmatch(policy.target_pattern, target)
    ):
        return unparsed
    return ModelRemoteLaneNeeds(
        action=EnumLaneNeedsAction(head), target=target, parsed=True, raw=first
    )


def report_text(final_message: str | None, policy: ModelRemoteLaneNeedsPolicy) -> str:
    """The lane's report as one cell value: whitespace collapsed, ``|`` made ``/``, cut to the policy bound."""
    text = " ".join(_report(final_message).replace("|", "/").split())
    return text[: policy.report_chars]
