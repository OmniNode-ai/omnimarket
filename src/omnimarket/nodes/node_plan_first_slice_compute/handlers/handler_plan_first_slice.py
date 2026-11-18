# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a plan in review names its first slice section, and how full it is.

``check_plan_first_slice(ModelPlanFirstSliceRequest) -> ModelPlanFirstSliceResult``
scans the plan's markdown headings (a line starting with 1 to 6 ``#`` then a space)
for the FIRST one naming a first slice section, outside fenced code blocks. A
heading matches when its text, lower-cased with leading numbering (``3.``,
``3.1``) and surrounding punctuation stripped, starts with one of the settings'
``first_slice_headings`` (compared lower-case). The section is the lines after
the heading up to the next heading of the same or higher level (fewer or equal
``#``), or to a heading naming a ``later_rung_headings`` entry, or to the end of
the text. Its words are the whitespace-split tokens of its lines, ignoring blank
lines and lines that are only a heading. No match is
``no_first_slice_section``; a match under ``min_section_words`` is
``empty_first_slice_section``; otherwise ``ok``. Pure: no clock, no I/O, no
model. Every setting comes from the request, the injected settings or the overlay.
"""

from __future__ import annotations

import re
import string
from typing import Final

from omnimarket.nodes.node_plan_first_slice_compute.models.model_plan_first_slice import (
    EnumPlanFirstSliceVerdict,
    ModelPlanFirstSliceRequest,
    ModelPlanFirstSliceResult,
    ModelPlanFirstSliceSettings,
)
from omnimarket.nodes.node_plan_first_slice_compute.overlay import load_settings

# A markdown heading: 1 to 6 '#' then a space, then the heading's text.
HEADING_RE: Final = re.compile(r"^(#{1,6})[ \t](.*)$")
# A fenced code block opens and closes on a line starting with ```; headings
# inside a fence are the block's text, never the plan's structure.
FENCE_PREFIX: Final[str] = "```"
# The leading numbering a heading may carry ("3.", "3.1", "3.1.4", "3)") before
# its text; surrounding punctuation is stripped separately.
LEADING_NUMBERING_RE: Final = re.compile(r"^\d+(?:\.\d+)*[.):\]]*\s+")


def _normalized_heading_text(text: str) -> str:
    """The heading's text as it is compared: lower-cased, its leading numbering
    (``3.``, ``3.1``) and surrounding punctuation stripped."""
    value = text.strip().lower().strip(string.punctuation)
    return LEADING_NUMBERING_RE.sub("", value, count=1).strip()


def _names_any(normalized: str, entries: tuple[str, ...]) -> bool:
    """Whether ``normalized`` starts with any lower-cased ``entries`` entry."""
    return any(normalized.startswith(entry) for entry in entries)


def check_plan_first_slice(
    request: ModelPlanFirstSliceRequest,
    settings: ModelPlanFirstSliceSettings,
) -> ModelPlanFirstSliceResult:
    """The plan's first-slice verdict under ``settings``: which heading named the section,
    how many words it carries, and one plain sentence naming it."""
    first = tuple(entry.lower() for entry in settings.first_slice_headings)
    later = tuple(entry.lower() for entry in settings.later_rung_headings)
    heading: str | None = None
    level = 0
    words = 0
    in_fence = False
    for line in request.plan_text.splitlines():
        if line.lstrip().startswith(FENCE_PREFIX):
            in_fence = not in_fence
            continue  # a fence delimiter is the block's structure, not the section's words
        if in_fence:
            if heading is not None and line.strip():
                words += len(line.split())
            continue
        hit = HEADING_RE.match(line)
        if hit is not None:
            marks, text = len(hit[1]), hit[2].strip()
            normalized = _normalized_heading_text(text)
            if heading is None:
                if _names_any(normalized, first):
                    heading, level, words = text, marks, 0
            elif marks <= level or _names_any(normalized, later):
                break  # the section ends here; the first match is the only one read
        elif heading is not None and line.strip():
            words += len(line.split())
    if heading is None:
        return ModelPlanFirstSliceResult(
            verdict=EnumPlanFirstSliceVerdict.NO_FIRST_SLICE_SECTION,
            heading=None,
            section_words=0,
            detail="The plan names no first slice section.",
        )
    if words < settings.min_section_words:
        return ModelPlanFirstSliceResult(
            verdict=EnumPlanFirstSliceVerdict.EMPTY_FIRST_SLICE_SECTION,
            heading=heading,
            section_words=words,
            detail=(
                f"The plan's first slice section {heading!r} carries "
                f"{words} words, under the minimum of {settings.min_section_words}."
            ),
        )
    return ModelPlanFirstSliceResult(
        verdict=EnumPlanFirstSliceVerdict.OK,
        heading=heading,
        section_words=words,
        detail=f"The plan's first slice section {heading!r} carries {words} words.",
    )


class HandlerPlanFirstSlice:
    """Whether a plan in review names its first slice section: definition-B compute.

    The settings come from the request, else from the ones injected at construction,
    else from the overlay, read once at the first call that needs them.
    """

    def __init__(self, settings: ModelPlanFirstSliceSettings | None = None) -> None:
        self._settings = settings

    def handle(self, request: ModelPlanFirstSliceRequest) -> ModelPlanFirstSliceResult:
        settings = request.settings or self._settings
        if settings is None:
            settings = self._settings = load_settings()
        return check_plan_first_slice(request, settings)


__all__: list[str] = [
    "HandlerPlanFirstSlice",
    "check_plan_first_slice",
]
