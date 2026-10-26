# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flat requests and results for account effects and cloud session text."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelCloudHost(BaseModel):
    """A caller supplied host row; consent is explicit."""

    model_config = _FROZEN
    name: str
    engines: tuple[str, ...] = ()
    engines_live: bool = False
    cloud_consent: str | None = None
    push: bool = True
    contributor: bool = False


class ModelCloudSessionPlacementRequest(BaseModel):
    """One action; the handler reports missing action inputs as errors."""

    model_config = _FROZEN
    action: Literal[
        "route",
        "slots",
        "pick",
        "note_start",
        "note_refusal",
        "release",
        "record_rate",
        "record_credit",
        "record_run",
        "mark_limited",
        "credit",
        "render_prompt",
        "render_start_script",
        "render_session_script",
        "render_pr_open_script",
        "render_bot_pr",
        "render_result",
        "parse_start",
        "parse_session",
        "parse_login",
        "parse_pr_url",
        "session_branches",
    ]
    placement_dir: str = ""
    now: datetime | None = None
    hosts: tuple[ModelCloudHost, ...] = ()
    claude_engines: tuple[str, ...] = ("claude_sonnet", "claude_opus")
    run_id: str = ""
    account: str = ""
    probes: dict[str, str] = Field(default_factory=dict)
    why: str = ""
    until: str = ""
    remaining: float | None = None
    source: str = ""
    rate: dict[str, Any] | None = None
    run: dict[str, Any] | None = None
    brief: str = ""
    repo: str | None = None
    model: str = ""
    placement: str = "auto"
    pinned_host: str | None = None
    inputs: tuple[str, ...] = ()
    excluded_repos: tuple[str, ...] = ()
    text: str = ""
    path_export: str = ""
    registry_root: str = ""
    owner: str = ""
    landing_branch: str = ""
    prompt_path: str = ""
    session_id: str = ""
    session_url_base: str = ""
    sessions_api_url: str = ""
    lane: str = ""
    ticket: str = ""
    branch: str = ""
    pr_heads: tuple[dict[str, str], ...] = ()
    timeout_min: int | None = None
    title: str = ""
    body_path: str = ""
    bus_lane: str = ""
    session: dict[str, str] = Field(default_factory=dict)
    prs: tuple[dict[str, str], ...] = ()
    ended: dict[str, Any] | None = None
    session_branches: tuple[str, ...] = ()
    opened: str | None = None
    state: dict[str, Any] | None = None


class ModelCloudSessionPlacementResult(BaseModel):
    """The action's answer, including probe continuations and input errors."""

    model_config = _FROZEN
    action: str
    error: str | None = None
    refusal: str | None = None
    lane_kind: str | None = None
    free: int = 0
    accounts: dict[str, int] = Field(default_factory=dict)
    caps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    account: str | None = None
    needs_probe: str | None = None
    tried: list[dict[str, str]] = Field(default_factory=list)
    cap: int | None = None
    held: str | None = None
    entry: dict[str, Any] | None = None
    credit: dict[str, Any] | None = None
    text: str | None = None
    title: str | None = None
    parsed: dict[str, Any] | None = None
    branches: list[str] = Field(default_factory=list)
