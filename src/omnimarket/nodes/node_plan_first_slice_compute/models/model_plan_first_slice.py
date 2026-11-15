# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request, result and settings of the plan first-slice check.

The settings name the headings that mark a plan's first slice section, the
word count that makes it more than a stub, and the headings that end it for a
later rung of the ladder. The overlay fills them at composition (the packaged
contract's ``config:`` section is the default), the request carries them beside
the plan's text, and the result carries the verdict, the matched heading, the
section's word count and one plain sentence of detail.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.enum_plan_first_slice_verdict import EnumPlanFirstSliceVerdict


class ModelPlanFirstSliceSettings(BaseModel):
    """The first-slice names and fullness bar a plan in review is checked against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_slice_headings: tuple[str, ...] = Field(
        description=(
            "Heading texts (compared lower-case) that name a first slice section."
        ),
        min_length=1,
    )
    min_section_words: int = Field(
        description="Fewer words in the section is an empty first slice section.",
        ge=1,
    )
    later_rung_headings: tuple[str, ...] = Field(
        default=(),
        description=(
            "Heading texts (compared lower-case) that end the first slice section "
            "even at a deeper level."
        ),
    )


class ModelPlanFirstSliceRequest(BaseModel):
    """One plan in review and the settings in force."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_text: str = Field(description="The plan document's full markdown text.")
    settings: ModelPlanFirstSliceSettings | None = Field(
        default=None,
        description=(
            "The first-slice headings, word minimum and later-rung headings. Absent: "
            "the handler's injected settings, else the overlay's (the packaged "
            "contract's config section when no overlay is configured)."
        ),
    )


class ModelPlanFirstSliceResult(BaseModel):
    """Whether the plan names a first slice section, and how full it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumPlanFirstSliceVerdict = Field(
        description="ok, no_first_slice_section or empty_first_slice_section."
    )
    heading: str | None = Field(
        description=(
            "The matched heading's text as written, or None when no heading matched."
        )
    )
    section_words: int = Field(
        description="The section's word count; 0 when no heading matched."
    )
    detail: str = Field(
        description="One plain sentence naming the heading or its absence."
    )


__all__: list[str] = [
    "EnumPlanFirstSliceVerdict",
    "ModelPlanFirstSliceRequest",
    "ModelPlanFirstSliceResult",
    "ModelPlanFirstSliceSettings",
]
