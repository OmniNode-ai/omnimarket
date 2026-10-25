# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly retrospective ends the Friday stand-up (OMN-21286).

RULING 2026-10-20T13:51:27Z, amended to Friday by RULING 2026-10-20T13:58:45Z. Entries are a JSON list of
``{role, product_use, idea | friction, process_change}``. Only those fields are read, so a name an entry carries never
reaches the sheet; credit is the role. Drafts are never filed here.
"""

import json
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_standup_retrospective_compute.models.model_standup_retrospective import (
    ModelStandupRetrospectiveRequest,
    ModelStandupRetrospectiveResult,
)

FRIDAY = 4
RETRO_FIELDS = ("product_use", "idea_or_friction", "process_change")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _error(detail: str) -> str:
    return f"retrospective: {detail.splitlines()[0] if detail else 'unparsable input'}"


def retrospective_lines(
    retrospective: dict[str, Any], drafts: list[dict[str, Any]]
) -> list[str]:
    """The text sheet's retrospective and ticket-draft sections; none when there is no retrospective."""
    if not retrospective:
        return []
    people = retrospective["people"]
    lines = [f"retrospective ({len(people)}):"]
    for row in people:
        parts = [
            f"use: {row['product_use'] or 'not reported'}",
            f"{row['kind'] or 'idea or friction'}: {row['idea_or_friction'] or 'not reported'}",
            f"process change: {row['process_change'] or 'not reported'}",
        ]
        lines.append(f"  {row['role']}: {'; '.join(parts)}")
    lines.append(f"ticket drafts ({len(drafts)}, not filed):")
    lines += [
        f"  [{draft['state']}] {draft['title']} (credit: {draft['credit']})"
        for draft in drafts
    ]
    return lines


class HandlerStandupRetrospective:
    def handle(
        self, request: ModelStandupRetrospectiveRequest
    ) -> ModelStandupRetrospectiveResult:
        result = ModelStandupRetrospectiveResult()
        if datetime.fromisoformat(request.until).weekday() != FRIDAY:
            if request.entries is not None:
                result.notes.append("not a Friday: no retrospective section")
            return result
        people: list[dict[str, Any]] = []
        result.retrospective.update(friday=True, people=people)
        if request.entries is None:
            result.notes.append(
                "Friday: no retrospective input (--retro); the section is empty"
            )
            return result
        try:
            entries = json.loads(request.entries)
        except ValueError as error:
            result.errors.append(_error(str(error)))
            return result
        if not isinstance(entries, list):
            result.errors.append(_error("input is not a JSON list of entries"))
            return result
        for number, entry in enumerate(entries, 1):
            role = _text(entry.get("role")) if isinstance(entry, dict) else ""
            if not role:
                result.errors.append(_error(f"entry {number} has no role; skipped"))
                continue
            idea, friction = _text(entry.get("idea")), _text(entry.get("friction"))
            row: dict[str, Any] = {
                "role": role,
                "product_use": _text(entry.get("product_use")),
                "idea_or_friction": idea or friction,
                "kind": "idea" if idea else "friction" if friction else "",
                "process_change": _text(entry.get("process_change")),
            }
            row["missing"] = [key for key in RETRO_FIELDS if not row[key]]
            people.append(row)
            if row["idea_or_friction"]:
                result.ticket_drafts.append(
                    {
                        "title": row["idea_or_friction"].splitlines()[0][:120],
                        "state": "Backlog",
                        "kind": row["kind"],
                        "credit": role,
                        "description": (
                            f"{row['idea_or_friction']}\n\nContributed at the Friday "
                            f"retrospective ending {request.until}, credited to the {role}."
                        ),
                        "filed": False,
                    }
                )
        return result
