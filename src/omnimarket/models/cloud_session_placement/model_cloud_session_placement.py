# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Flat requests and results for account effects and cloud session text."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelCloudAccount(BaseModel):
    """One claude.ai login identity, from the overlay's ``accounts[]``. Every key is checked by the registry loader,
    which names the absent one; the defaults only let the loader, not pydantic, word the refusal."""

    model_config = _FROZEN
    label: str = ""
    org_hash: str = ""
    email_hash: str = ""
    consent: str = ""
    consent_expires: str = ""
    credit_expires: str = ""
    legacy_hosts: tuple[str, ...] = ()
    transport_exclude: tuple[str, ...] = ()


class ModelCloudTransportRow(BaseModel):
    """A raw host table row handed to ``transports``. Fields the node does not read (a host's lab ``available``
    among them) are ignored, never consulted."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    name: str
    target: str = ""
    local: bool = False
    push: bool = True
    contributor: bool = False


class ModelCloudTransport(BaseModel):
    """A host a cloud start call may be made from (the output of ``transports``, the input of ``pick``)."""

    model_config = _FROZEN
    name: str
    target: str = ""
    local: bool = False


class ModelCloudLoginObservation(BaseModel):
    """The raw texts the caller got from one host: ``claude auth status --json`` and the cloud probe. The node parses
    both; an unreachable host carries no texts."""

    model_config = _FROZEN
    host: str
    reachable: bool = True
    auth_text: str = ""
    probe_text: str = ""


class ModelCloudSessionPlacementRequest(BaseModel):
    """One action; the handler reports missing action inputs as errors."""

    model_config = _FROZEN
    action: Literal[
        "route",
        "slots",
        "transports",
        "observe_logins",
        "pick",
        "start_outcome",
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
    accounts: tuple[ModelCloudAccount, ...] = ()
    login_fresh_s: int | None = None
    probe_timeout_s: int | None = None
    rows: tuple[ModelCloudTransportRow, ...] = ()
    transports: tuple[ModelCloudTransport, ...] = ()
    observations: tuple[ModelCloudLoginObservation, ...] = ()
    transport: str = ""
    began: bool = False
    untried: tuple[str, ...] = ()
    run_id: str = ""
    account: str = ""
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
    order: list[str] = Field(default_factory=list)
    free: int = 0
    accounts: dict[str, int] = Field(default_factory=dict)
    caps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    account: str | None = None
    transport: str | None = None
    needs_probe: str | None = None
    tried: list[dict[str, str]] = Field(default_factory=list)
    sentences: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    transports: list[dict[str, Any]] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    logins: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    unconsented: list[dict[str, str]] = Field(default_factory=list)
    unreachable: list[str] = Field(default_factory=list)
    stale_hosts: list[str] = Field(default_factory=list)
    probe_timeout_s: int | None = None
    verdict: str | None = None
    sentence: str | None = None
    next_transport: str | None = None
    cap: int | None = None
    held: str | None = None
    entry: dict[str, Any] | None = None
    credit: dict[str, Any] | None = None
    text: str | None = None
    title: str | None = None
    parsed: dict[str, Any] | None = None
    branches: list[str] = Field(default_factory=list)
