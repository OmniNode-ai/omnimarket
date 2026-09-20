# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B input for the lab lane-health fold (OMN-18769)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLabLaneHealthRequest(BaseModel):
    """One source fact, named by the topic it arrived on.

    The three input topics carry three unrelated payload shapes — a census
    plan, a runtime health snapshot, a lab-pass receipt. Modelling their union
    as one flat optional-everything record would make every field optional and
    let a malformed census validate as an empty health event. Instead the
    topic names the shape, and the fold's per-shape parsers (``census_facts``,
    ``health_fact``, ``receipt_fact``) each refuse what they cannot read.

    ``extra="ignore"`` on the payload is not needed because ``payload`` is a
    mapping carried whole; the parsers read the keys they declare and the rest
    is none of this node's business.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    topic: str = Field(
        description=(
            "The source topic. Must be one of the contract's subscribe_topics; "
            "an unsubscribed topic is refused rather than best-effort parsed."
        )
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="The source event, verbatim, as the fold's parsers read it",
    )
