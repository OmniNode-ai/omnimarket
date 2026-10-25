# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for lane-brief's compact brief form (compact and expand)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict


class ModelBriefCompactRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # compact | expand
    operation: str
    brief: str
    # lane-brief's compact_render: the full text a reference stands for
    render: Callable[[str, Mapping[str, str]], str]
    # compact: the brief's 3a.8 CLAIM template, whose cells parameterise the references
    claim: str = ""
    names: tuple[str, ...] = ("rules", "landing-fixed", "delegation", "heavy-work")
    # compact: the standing rules block's heading, kept inline
    rules_head: str = ""
    # compact: the rules block's dispatch paragraphs' prefixes, kept inline
    dispatch_rules: tuple[str, ...] = ()
    # expand: refuse a reference whose sha256 differs from the fresh rendering
    verify: bool = True


class ModelBriefCompactResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    brief: str
