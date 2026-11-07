# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the create payload of one floor task's ticket from the plan text, or name what the plan lacks.

A pure compute: no clock, no I/O and no Linear call. The payload is built from the plan, never from the floor
note: the title from the task heading, the parent from the plan header's ticket line, the task's own labelled
criteria copied with their falsifiers, a ``Gate:`` line, a ``Definition of done`` block that names the merge, the dev
deployment and the integration check the task's Lab step names, and one ``Floor key:`` line.

A task that lacks a criterion with a falsifier or a named integration check is ``NEEDS-SPEC`` (the parts are named),
a plan whose header names no ticket is ``NO-PARENT``, and a plan that could not be read is ``PLAN-UNREAD``; the first
two make no Linear call because the node makes none. The payload declares no project, no label and no pillar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayload,
    ModelFloorTaskTicketPayloadRequest,
    ModelFloorTaskTicketPayloadResult,
    floor_key,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models.model_floor_task_ticket_payload import (
    CAUSE_NEEDS_SPEC,
    CAUSE_NO_PARENT,
    CAUSE_PLAN_UNREAD,
)

FLOOR_KEY_LABEL = "Floor key:"
GATE_LINE = "Gate: {parent} AC-1"
PART_TASK_SECTION = "task-section"
PART_CRITERIA = "criteria"
PART_INTEGRATION = "integration-check"
PART_FALSIFIER = "falsifier:"

_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*$")
_FIELD_BULLET = re.compile(r"^[-*][ \t]+\*\*(?P<name>[^*]+?)\*\*(?P<rest>.*)$")
_HEADER_TICKET = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?\*{0,2}ticket\*{0,2}[ \t]*:?\*{0,2}[ \t]*(?P<value>.*)$",
    re.IGNORECASE,
)
_PARENT_AT_START = re.compile(r"^\[?(?P<ticket>OMN-[0-9]+)\b")
_NUMBERED_ITEM = re.compile(r"^[ \t]+(?P<ordinal>[0-9]+)\.[ \t]+(?P<text>\S.*)$")
_CHECKBOX_ITEM = re.compile(r"^[ \t]*[-*][ \t]+\[[ xX]\][ \t]+(?P<text>\S.*)$")
_FALSIFIER = re.compile(r"falsifier[ \t]*:|falsified by", re.IGNORECASE)
_NONE_LAB = re.compile(r"^none\b", re.IGNORECASE)
_NAMES_ANOTHER_CHECK = re.compile(
    r"\bT[0-9]+(?:\.[0-9]+)?\b|integration", re.IGNORECASE
)
_MARKDOWN_NOISE = re.compile(r"[`*_]")

CRITERIA_FIELD = "acceptance criteria"
LAB_FIELD = "lab step"
INTEGRATION_FIELD = "integration check"


@dataclass(frozen=True)
class _Criterion:
    ordinal: int
    text: str


def _header_ticket(plan_text: str) -> str | None:
    """The ticket the plan header's ``Ticket:`` line starts with, or None (for example ``none yet``)."""
    for line in plan_text.splitlines():
        heading = _HEADING.match(line)
        if heading is not None and len(heading["hashes"]) >= 2:
            return None
        found = _HEADER_TICKET.match(line)
        if found is None:
            continue
        value = _MARKDOWN_NOISE.sub("", found["value"]).strip()
        parent = _PARENT_AT_START.match(value)
        return parent["ticket"] if parent is not None else None
    return None


def _task_section(plan_text: str, task_id: str) -> tuple[str, list[str]] | None:
    """The heading text after the task id and the body lines of the task's section, or None."""
    heading_re = re.compile(
        rf"^#{{1,6}}[ \t]+{re.escape(task_id)}(?:[.:][ \t]*|[ \t]+)(?P<title>.*?)[ \t]*$"
    )
    lines = plan_text.splitlines()
    for index, line in enumerate(lines):
        opened = heading_re.match(line)
        if opened is None:
            continue
        level = len(line) - len(line.lstrip("#"))
        body: list[str] = []
        for later in lines[index + 1 :]:
            next_heading = _HEADING.match(later)
            if next_heading is not None and len(next_heading["hashes"]) <= level:
                break
            body.append(later)
        return opened["title"], body
    return None


def _fields(body: list[str]) -> dict[str, list[str]]:
    """The top-level ``- **Name:** ...`` bullets of a task section, each with its following lines."""
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body:
        bullet = _FIELD_BULLET.match(line)
        if bullet is not None:
            name = bullet["name"].strip().rstrip(":").strip().lower()
            current = fields.setdefault(name, [])
            rest = bullet["rest"].strip().lstrip(":").strip()
            if rest:
                current.append(rest)
        elif current is not None:
            current.append(line)
    return fields


def _criteria(lines: list[str]) -> list[_Criterion]:
    """The labelled criteria of the Acceptance criteria field, each with its continuation lines joined."""
    items: list[list[str]] = []
    for line in lines:
        numbered = _NUMBERED_ITEM.match(line)
        checkbox = _CHECKBOX_ITEM.match(line)
        if numbered is not None:
            items.append([numbered["text"].strip()])
        elif checkbox is not None:
            items.append([checkbox["text"].strip()])
        elif line.strip() and items:
            items[-1].append(line.strip())
    return [
        _Criterion(ordinal=position, text=" ".join(parts))
        for position, parts in enumerate(items, start=1)
    ]


def _integration_check(fields: dict[str, list[str]]) -> str | None:
    """The integration check the task names: an Integration check field, or a Lab step that is not just ``none``."""
    for name in (INTEGRATION_FIELD, LAB_FIELD):
        text = " ".join(line.strip() for line in fields.get(name, []) if line.strip())
        if not text:
            continue
        if _NONE_LAB.match(text) and not _NAMES_ANOTHER_CHECK.search(text):
            continue
        return text
    return None


def _description(
    key: str,
    parent: str,
    task_id: str,
    title: str,
    criteria: list[_Criterion],
    integration: str,
) -> str:
    listed = "\n".join(f"- [ ] AC{c.ordinal}: {c.text}" for c in criteria)
    return (
        f"{GATE_LINE.format(parent=parent)}\n"
        f"\n"
        f"{FLOOR_KEY_LABEL} {key}\n"
        f"\n"
        f"Plan task {task_id}: {title}\n"
        f"\n"
        f"## Acceptance criteria\n"
        f"\n"
        f"{listed}\n"
        f"\n"
        f"## Definition of done\n"
        f"\n"
        f"- The change is merged on the landing branch of every repository the task changes"
        f" -- falsifier: the pull request of each repository reads merged on its landing branch\n"
        f"- The change is deployed on the dev lane"
        f" -- falsifier: the dev deployment evidence names the merged commit\n"
        f"- Integration check: {integration}"
        f" -- falsifier: the integration check is run and its output is read\n"
    )


class HandlerFloorTaskTicketPayload:
    """Compute the payload of one floor task's ticket, or the one verdict that says why none can be built."""

    def handle(
        self, request: ModelFloorTaskTicketPayloadRequest
    ) -> ModelFloorTaskTicketPayloadResult:
        if request.plan_text is None:
            return ModelFloorTaskTicketPayloadResult(
                verdict="PLAN-UNREAD", cause=CAUSE_PLAN_UNREAD
            )
        parent = _header_ticket(request.plan_text)
        if parent is None:
            return ModelFloorTaskTicketPayloadResult(
                verdict="NO-PARENT", cause=CAUSE_NO_PARENT
            )
        section = _task_section(request.plan_text, request.task_id)
        if section is None:
            return self._needs_spec((PART_TASK_SECTION,))
        title, body = section
        fields = _fields(body)
        criteria = _criteria(fields.get(CRITERIA_FIELD, []))
        missing: list[str] = []
        if not criteria:
            missing.append(PART_CRITERIA)
        missing.extend(
            f"{PART_FALSIFIER}{c.ordinal}"
            for c in criteria
            if _FALSIFIER.search(c.text) is None
        )
        integration = _integration_check(fields)
        if integration is None:
            missing.append(PART_INTEGRATION)
        if missing:
            return self._needs_spec(tuple(missing))
        assert integration is not None
        key = floor_key(request.line_id, request.task_id)
        task_title = f"{request.task_id}. {title}".strip()
        return ModelFloorTaskTicketPayloadResult(
            verdict="PAYLOAD",
            payload=ModelFloorTaskTicketPayload(
                floor_key=key,
                title=task_title,
                description=_description(
                    key, parent, request.task_id, title, criteria, integration
                ),
                parent=parent,
            ),
        )

    @staticmethod
    def _needs_spec(parts: tuple[str, ...]) -> ModelFloorTaskTicketPayloadResult:
        return ModelFloorTaskTicketPayloadResult(
            verdict="NEEDS-SPEC",
            missing=parts,
            cause=CAUSE_NEEDS_SPEC + ",".join(parts),
        )
