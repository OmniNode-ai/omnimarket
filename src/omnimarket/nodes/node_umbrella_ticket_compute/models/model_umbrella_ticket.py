# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the umbrella-ticket refusal of a PR-opening lane or a PR create (OMN-21301)."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict


class ModelUmbrellaTicketRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # the lane's --ticket and its brief CLAIM ticket, or the ids a PR title cites
    tickets: tuple[str, ...]
    # the brief or the PR body, read for the `Umbrella evidence: <reason>` line
    text: str
    # the lane may open or push a PR (a PR create always does); a read-only lane is never refused
    opens_pr: bool
    # the umbrella ticket set; None reads setting umbrella_tickets (ONEX_UMBRELLA_TICKETS, else the overlay)
    umbrella_tickets: tuple[str, ...] | None = None
    # the environment the setting is read from; None reads os.environ
    environ: Mapping[str, str] | None = None
    # "lane" (lane-brief check, remote-lane runner) or "pr" (guard-create): which fix it names
    surface: str = "lane"


class ModelUmbrellaTicketResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # empty when admitted; one refusal line naming the fix otherwise
    refusals: tuple[str, ...]
