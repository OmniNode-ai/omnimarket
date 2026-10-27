# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the remote-lane runner's pre-placement brief and lane-shape refusals."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelDispatchBriefShapeRequest:
    brief: str
    lane: str
    prs: tuple[str, ...]
    lane_brief_path: str  # lane-brief's lane_brief.py, whose prose readers and mint rule this node applies
    ticket: str | None = (
        None  # the dispatch's --ticket; the brief's own CLAIM ticket is read from the brief
    )
    repo: str | None = None  # the dispatch's --repo; a lane with a repo may open a PR


@dataclass(frozen=True)
class ModelDispatchBriefShapeResult:
    refusals: tuple[
        tuple[str, str], ...
    ]  # (receipt status, REMOTE-LANE-* line); the first one refuses the lane
