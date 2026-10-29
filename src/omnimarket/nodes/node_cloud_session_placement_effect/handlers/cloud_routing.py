# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud eligibility decisions using caller supplied repository exclusions."""

from collections.abc import Collection, Mapping, Sequence
from importlib.resources import files

import yaml

from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    lane_model_refusal,
)

from .cloud_session import lane_kind

CLAUDE_MODELS = {"sonnet": "claude_sonnet"}
CLOUD_LANE_KINDS = frozenset(
    {"conflict-fix", "red-test-fix", "small-code", "plan-draft"}
)


def cloud_refusal(
    brief: str,
    *,
    repo: str | None,
    model: str,
    placement: str,
    pinned_host: str | None,
    inputs: Sequence[str],
    excluded_repos: Collection[str],
    lane_kinds: Collection[str] = CLOUD_LANE_KINDS,
    models: Mapping[str, str] = CLAUDE_MODELS,
) -> str | None:
    """Why this lane may not run as a cloud session, or None when it may."""
    if placement == "lab":
        return "--placement lab"
    if refusal := lane_model_refusal(model):
        return refusal
    if model not in models:
        return f"--model {model} is not a Claude Code lane"
    if not repo:
        return "no --repo: a cloud session works one GitHub repository"
    if repo in excluded_repos:
        return f"{repo} is excluded from cloud sessions"
    kind = lane_kind(brief)
    if kind is None:
        return "the brief declares no `Lane kind:` line"
    if kind not in lane_kinds:
        return f"lane kind {kind} is not repo-only ({', '.join(sorted(lane_kinds))})"
    if pinned_host or inputs:
        return f"the lane needs a lab host ({pinned_host or 'host-local inputs'})"
    return None


def placement_order(placement: str) -> list[str]:
    """The places to try for this placement, in order, as the node's contract declares them."""
    contract = yaml.safe_load(
        files(__package__.rsplit(".", 1)[0]).joinpath("contract.yaml").read_text()
    )
    order = contract["placement_order"][placement]
    if not order or not all(isinstance(place, str) for place in order):
        raise ValueError(f"placement_order.{placement} names no place")
    return list(order)
