# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketPlanCompute.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12233
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_request import (
    ModelTicketPlanRequest,
)
from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_result import (
    ModelTicketPlanResult,
    ModelTicketSpec,
)

_DEPENDENCY_RE = re.compile(r"\b(?:depends on|blocked by|after)\s*[:\-]?\s*(.+)$", re.I)
_LABEL_RE = re.compile(r"\b(?:labels?|tags?)\s*[:=]\s*([A-Za-z0-9_, ./-]+)", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<body>.+?)\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(?P<title>.+?)\s*$")


class HandlerTicketPlanCompute:
    """Parse simple markdown plans into deterministic ticket specs."""

    def handle(self, request: ModelTicketPlanRequest) -> ModelTicketPlanResult:
        phase: str | None = None
        tickets: list[ModelTicketSpec] = []
        warnings: list[str] = []

        for line_number, raw_line in enumerate(request.plan_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                phase = heading_match.group("title").strip(" :")
                continue

            bullet_match = _BULLET_RE.match(raw_line)
            if not bullet_match:
                continue

            ticket = _parse_ticket_bullet(
                bullet_match.group("body"),
                phase=phase,
                line_number=line_number,
            )
            if ticket is None:
                warnings.append(f"line {line_number}: skipped empty ticket bullet")
                continue
            tickets.append(ticket)

        if not tickets:
            warnings.append("no ticket bullets found")

        return ModelTicketPlanResult(tickets=tickets, parse_warnings=warnings)


def _parse_ticket_bullet(
    body: str, *, phase: str | None, line_number: int
) -> ModelTicketSpec | None:
    text = body.strip()
    if not text:
        return None

    title_part, description_part = _split_title_description(text)
    title = _strip_checkbox(title_part).strip()
    if not title:
        title = f"Untitled ticket from line {line_number}"

    depends_on = _extract_dependencies(description_part)
    labels = _extract_labels(description_part)
    description = _clean_description(description_part)

    return ModelTicketSpec(
        title=title,
        description=description,
        phase=phase,
        depends_on=depends_on,
        labels=labels,
    )


def _split_title_description(text: str) -> tuple[str, str]:
    for separator in (" — ", " -- ", ": "):
        if separator in text:
            title, description = text.split(separator, 1)
            return title, description
    return text, ""


def _strip_checkbox(text: str) -> str:
    return re.sub(r"^\[[ xX]\]\s*", "", text)


def _extract_dependencies(description: str) -> list[str]:
    match = _DEPENDENCY_RE.search(description)
    if not match:
        return []
    return [item.strip() for item in re.split(r",|;", match.group(1)) if item.strip()]


def _extract_labels(description: str) -> list[str]:
    match = _LABEL_RE.search(description)
    if not match:
        return []
    return [item.strip() for item in re.split(r",|;", match.group(1)) if item.strip()]


def _clean_description(description: str) -> str:
    description = _DEPENDENCY_RE.sub("", description)
    description = _LABEL_RE.sub("", description)
    return description.strip(" ;,-")
