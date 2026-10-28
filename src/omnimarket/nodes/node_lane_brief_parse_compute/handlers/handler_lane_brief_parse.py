# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse a lane brief's column-zero metadata lines into ``ModelLaneBrief``.

Only a line that starts in column zero outside a code fence is metadata. The engine line must hold
one word of ``EnumLaneEngine``; a missing, repeated, empty or prose engine line refuses the brief and
the issue names the line. The header the assemble operation writes (``# Lane <lane>: <title>`` and the
``Parent: ... Ticket: ... Repo: ...`` line) supplies the task, the ticket and the repository when the
brief has no line of its own. Pure: reads no file, calls no model and takes ``now`` from the request.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from omnimarket.models.lane_brief.enum_lane_engine import EnumLaneEngine
from omnimarket.models.lane_brief.model_lane_brief import ModelLaneBrief
from omnimarket.models.lane_brief.model_lane_brief_issue import ModelLaneBriefIssue
from omnimarket.models.lane_brief.model_lane_brief_parse_request import (
    ModelLaneBriefParseRequest,
)
from omnimarket.models.lane_brief.model_lane_brief_parse_result import (
    ModelLaneBriefParseResult,
)
from omnimarket.models.lane_brief_draft.model_brief_quoted_criteria import (
    ModelBriefQuotedCriteria,
)

_ENGINE = "Required engine"
_HOST = "Required host"
_OPENS = "Opens PR in"
_PROVES = "Proves"
_TICKET = "Ticket"
_TASK = "Task"
_FENCE = re.compile(r"^(```|~~~)")
_METADATA = re.compile(r"^([A-Z][A-Za-z ]*?):[ \t]*(.*?)[ \t]*$")
_TITLE = re.compile(r"^# Lane [^:\n]+:[ \t]*(.+?)[ \t]*$")
_HEADER = re.compile(r"^Parent: .*?\bTicket: (OMN-[0-9]+)\. Repo: ([^ ]+?)\.")
_CRITERIA = re.compile(r"^ticket-criteria:[ \t]*(\S+)[ \t]*$")
_ALIASES = {"sonnet": "claude-sonnet", "opus": "claude-opus"}


def _column_zero_lines(brief: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in brief.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def _engine_of(
    values: list[str],
) -> tuple[EnumLaneEngine | None, ModelLaneBriefIssue | None]:
    if not values:
        return None, ModelLaneBriefIssue(line=_ENGINE, detail="no such line")
    if len(values) > 1:
        return None, ModelLaneBriefIssue(
            line=_ENGINE, detail=f"stated {len(values)} times: {values!r}"
        )
    word = values[0].strip().lower().replace("_", "-")
    word = _ALIASES.get(word, word)
    try:
        return EnumLaneEngine(word), None
    except ValueError:
        return None, ModelLaneBriefIssue(
            line=_ENGINE,
            detail=f"not one engine of {[e.value for e in EnumLaneEngine]}: {values[0]!r}",
        )


def _criteria_of(lines: list[str]) -> tuple[ModelBriefQuotedCriteria, ...]:
    found: list[ModelBriefQuotedCriteria] = []
    for index, line in enumerate(lines):
        match = _CRITERIA.match(line)
        if match is None:
            continue
        quoted: list[str] = []
        for following in lines[index + 1 :]:
            if not following.startswith("> "):
                break
            quoted.append(following[2:])
        try:
            found.append(
                ModelBriefQuotedCriteria(ticket=match.group(1), lines=tuple(quoted))
            )
        except ValidationError:
            continue
    return tuple(found)


class HandlerLaneBriefParse:
    """Pure: the same brief and ``now`` give the same result."""

    def handle(self, request: ModelLaneBriefParseRequest) -> ModelLaneBriefParseResult:
        lines = _column_zero_lines(request.brief)
        values: dict[str, list[str]] = {}
        for line in lines:
            match = _METADATA.match(line)
            if match is not None:
                values.setdefault(match.group(1), []).append(match.group(2))
        task = next((m.group(1) for line in lines if (m := _TITLE.match(line))), "")
        header = next((m for line in lines if (m := _HEADER.match(line))), None)

        def one(name: str, fallback: str = "") -> str:
            found = values.get(name, [])
            return found[0] if found and found[0] else fallback

        engine, engine_issue = _engine_of(values.get(_ENGINE, []))
        issues: list[ModelLaneBriefIssue] = [engine_issue] if engine_issue else []
        fields = {
            "task": task or one(_TASK),
            "required_host": one(_HOST),
            "ticket": one(_TICKET, header.group(1) if header else ""),
            "opens_pr_in": one(_OPENS, header.group(2) if header else ""),
            "proves": one(_PROVES),
            "quoted_criteria": _criteria_of(lines),
        }
        labels = {"task": "Lane title", "ticket": _TICKET, "opens_pr_in": _OPENS}
        brief: ModelLaneBrief | None = None
        if engine is not None:
            try:
                brief = ModelLaneBrief(engine=engine, **fields)
            except ValidationError as error:
                issues.extend(
                    ModelLaneBriefIssue(
                        line=labels.get(str(err["loc"][0]), str(err["loc"][0])),
                        detail=f"{err['msg']}: {err['input']!r}",
                    )
                    for err in error.errors()
                )
        if issues or brief is None:
            return ModelLaneBriefParseResult(
                parsed=False, issues=tuple(issues), parsed_at=request.now
            )
        return ModelLaneBriefParseResult(
            parsed=True, brief=brief, parsed_at=request.now
        )
