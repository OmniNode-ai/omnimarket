# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInsightsToPlanCompute — HTML parse → extract insights → structure plan.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, cast

from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_request import (
    ModelInsightsToPlanComputeRequest,
)
from omnimarket.nodes.node_insights_to_plan_compute.models.model_insights_to_plan_compute_result import (
    ModelInsightsToPlanComputeResult,
)

_PRIORITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("high", "high"),
    ("urgent", "high"),
    ("critical", "high"),
    ("blocker", "high"),
    ("medium", "medium"),
    ("normal", "medium"),
    ("low", "low"),
)

_OWNER_PREFIXES = ("owner:", "team:", "assignee:")

_ACTION_PREFIXES = (
    "action:",
    "todo:",
    "task:",
    "next:",
    "fix:",
)

HeadingLevel = Literal["h1", "h2", "h3"]


class HandlerInsightsToPlanCompute:
    """Deterministically convert an HTML insights document into a plan."""

    def handle(
        self, request: ModelInsightsToPlanComputeRequest
    ) -> ModelInsightsToPlanComputeResult:
        html_path = Path(request.html_path)
        if not html_path.is_absolute():
            return ModelInsightsToPlanComputeResult(
                status="error",
                error="html_path must be absolute",
            )

        if not html_path.is_file():
            return ModelInsightsToPlanComputeResult(
                status="error",
                error=f"html_path does not reference a file: {request.html_path}",
            )

        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ModelInsightsToPlanComputeResult(
                status="error",
                error=f"failed to read HTML document: {exc}",
            )

        parser = _InsightsHtmlParser()
        parser.feed(html)
        parser.close()

        action_items = [
            item
            for item in (
                _action_item_for_text(text)
                for text in parser.bullets
                if _looks_like_action(text)
            )
            if item is not None
        ]
        sections = [
            {"level": level, "title": title}
            for level, title in parser.headings
            if title
        ]
        insights = [text for text in parser.paragraphs if text]

        plan: dict[str, object] = {
            "source_type": request.source_type,
            "title": parser.title or _title_from_path(html_path),
            "sections": sections,
            "insights": insights,
            "action_count": len(action_items),
        }

        return ModelInsightsToPlanComputeResult(
            status="ok",
            plan=plan,
            action_items=action_items,
        )


class _InsightsHtmlParser(HTMLParser):
    """Small purpose-built parser for deterministic text extraction."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[tuple[HeadingLevel, str]] = []
        self.paragraphs: list[str] = []
        self.bullets: list[str] = []
        self._capture: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"title", "h1", "h2", "h3", "p", "li"}:
            self._capture = tag
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture != tag:
            return

        text = _normalize_text("".join(self._buffer))
        if text:
            self._record_text(tag, text)

        self._capture = None
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    def _record_text(self, tag: str, text: str) -> None:
        if tag == "title":
            self.title = text
        elif tag in {"h1", "h2", "h3"}:
            self.headings.append((cast(HeadingLevel, tag), text))
        elif tag == "p":
            self.paragraphs.append(text)
        elif tag == "li":
            self.bullets.append(text)


def _action_item_for_text(text: str) -> dict[str, str] | None:
    description = _strip_action_prefix(text)
    if not description:
        return None

    return {
        "description": description,
        "priority": _priority_for_text(text),
        "owner": _owner_for_text(text),
    }


def _looks_like_action(text: str) -> bool:
    lowered = text.casefold().strip()
    return lowered.startswith(_ACTION_PREFIXES) or any(
        marker in lowered for marker in ("must ", "should ", "need to ", "needs ")
    )


def _strip_action_prefix(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.casefold()
    for prefix in _ACTION_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _priority_for_text(text: str) -> str:
    lowered = text.casefold()
    for marker, priority in _PRIORITY_MARKERS:
        if marker in lowered:
            return priority
    return "medium"


def _owner_for_text(text: str) -> str:
    tokens = text.replace(";", " ").split()
    for index, token in enumerate(tokens):
        lowered = token.casefold()
        for prefix in _OWNER_PREFIXES:
            if lowered.startswith(prefix):
                owner = token[len(prefix) :].strip()
                if owner:
                    return owner
                if index + 1 < len(tokens):
                    return tokens[index + 1].strip()
    return ""


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _title_from_path(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip()
