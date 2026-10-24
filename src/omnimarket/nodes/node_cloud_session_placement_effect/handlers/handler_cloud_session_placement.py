# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Account file effects and cloud session text, with caller performed probes."""

from pathlib import Path
from typing import Any

from ..models import (
    ModelCloudHost,
    ModelCloudSessionPlacementRequest,
    ModelCloudSessionPlacementResult,
)
from . import cloud_accounts as acc
from . import cloud_session as ses
from .cloud_routing import cloud_refusal


class _ProbeNeededError(Exception):
    """Suspend a pick before the caller's network round trip."""


_ACCOUNT_ACTIONS = frozenset(
    {
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
    }
)
_REQUIRED: dict[str, tuple[str, ...]] = {
    "note_start": ("account",),
    "note_refusal": ("account", "run_id", "why"),
    "release": ("account", "run_id"),
    "record_rate": ("account", "rate"),
    "record_credit": ("account", "remaining", "source"),
    "record_run": ("account", "run"),
    "mark_limited": ("account", "until"),
    "credit": ("account",),
    "render_prompt": (
        "brief",
        "lane",
        "ticket",
        "owner",
        "repo",
        "landing_branch",
        "branch",
        "run_id",
        "timeout_min",
    ),
    "render_start_script": (
        "path_export",
        "registry_root",
        "owner",
        "repo",
        "landing_branch",
        "run_id",
        "prompt_path",
    ),
    "render_session_script": ("path_export", "session_id", "sessions_api_url"),
    "parse_start": ("session_url_base",),
    "render_pr_open_script": (
        "registry_root",
        "owner",
        "repo",
        "lane",
        "branch",
        "landing_branch",
        "title",
        "body_path",
        "path_export",
        "bus_lane",
    ),
    "render_bot_pr": (
        "ticket",
        "lane",
        "session",
        "account",
        "run_id",
        "branch",
        "ended",
    ),
    "render_result": ("owner", "account", "session", "run_id"),
    "parse_pr_url": ("owner", "repo"),
    "session_branches": ("state", "landing_branch"),
}


class HandlerCloudSessionPlacement:
    """Perform one local effect or render/parse action and always report errors."""

    def handle(
        self, request: ModelCloudSessionPlacementRequest
    ) -> ModelCloudSessionPlacementResult:
        r = request
        required = _REQUIRED.get(r.action, ())
        if r.action in _ACCOUNT_ACTIONS:
            required = ("placement_dir", *required)
        for field in required:
            value = getattr(r, field)
            if value is None or value == "" or (field == "session" and not value):
                return ModelCloudSessionPlacementResult(
                    action=r.action, error=f"missing field: {field}"
                )
        if r.action in {"render_bot_pr", "render_result"} and not r.session.get("url"):
            return ModelCloudSessionPlacementResult(
                action=r.action, error="missing field: session.url"
            )
        try:
            return ModelCloudSessionPlacementResult(action=r.action, **self._handle(r))
        except _ProbeNeededError as exc:
            return ModelCloudSessionPlacementResult(
                action=r.action, needs_probe=str(exc)
            )
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            return ModelCloudSessionPlacementResult(action=r.action, error=str(exc))

    def _handle(self, r: ModelCloudSessionPlacementRequest) -> dict[str, Any]:
        pdir = Path(r.placement_dir)
        action = r.action
        if action == "route":
            return {
                "refusal": cloud_refusal(
                    r.brief,
                    repo=r.repo,
                    model=r.model,
                    placement=r.placement,
                    pinned_host=r.pinned_host,
                    inputs=r.inputs,
                    excluded_repos=r.excluded_repos,
                ),
                "lane_kind": ses.lane_kind(r.brief),
            }
        if action == "slots":
            consented = acc.consented_accounts(r.hosts)
            accounts = acc.cloud_slots(pdir, r.now, consented=consented)
            return {
                "free": sum(accounts.values()),
                "accounts": accounts,
                "caps": acc.cloud_caps(pdir, r.now, consented=consented),
            }
        if action == "pick":

            def probe(host: ModelCloudHost) -> str:
                if host.name not in r.probes:
                    raise _ProbeNeededError(host.name)
                return r.probes[host.name]

            pick, tried = acc.cloud_pick(
                pdir,
                r.hosts,
                probe,
                now=r.now,
                run_id=r.run_id,
                claude_engines=r.claude_engines,
            )
            return {"account": pick.name if pick else None, "tried": tried}
        if action == "note_start":
            return {"cap": acc.note_cloud_start(pdir, r.account, now=r.now)}
        if action == "note_refusal":
            return {
                "cap": acc.note_cloud_refusal(
                    pdir, r.account, r.run_id, r.why, now=r.now
                )
            }
        if action == "release":
            acc.release_cloud_slot(pdir, r.account, r.run_id)
        elif action == "record_rate":
            return {"held": acc.record_cloud_rate(pdir, r.account, r.rate, now=r.now)}
        elif action == "record_credit":
            assert r.remaining is not None
            return {
                "entry": acc.record_cloud_credit(
                    pdir, r.account, r.remaining, r.source, now=r.now
                )
            }
        elif action == "record_run":
            assert r.run is not None
            acc.record_cloud_run(pdir, r.account, r.run)
        elif action == "mark_limited":
            acc.mark_cloud_limited(pdir, r.account, r.until, r.why)
        elif action == "credit":
            return {"credit": acc.cloud_credit(pdir, r.account)}
        elif action == "render_prompt":
            assert r.repo is not None
            assert r.timeout_min is not None
            return {
                "text": ses.cloud_prompt(
                    r.brief,
                    lane=r.lane,
                    ticket=r.ticket,
                    owner=r.owner,
                    repo=r.repo,
                    landing_branch=r.landing_branch,
                    branch=r.branch,
                    pr_heads=r.pr_heads,
                    run_id=r.run_id,
                    timeout_min=r.timeout_min,
                )
            }
        elif action == "render_start_script":
            assert r.repo is not None
            return {
                "text": ses.cloud_start_script(
                    r.path_export,
                    r.registry_root,
                    r.owner,
                    r.repo,
                    r.landing_branch,
                    r.run_id,
                    r.prompt_path,
                )
            }
        elif action == "render_session_script":
            return {
                "text": ses.cloud_session_script(
                    r.path_export, r.session_id, r.sessions_api_url
                )
            }
        elif action == "render_pr_open_script":
            assert r.repo is not None
            return {
                "text": ses.cloud_pr_open_script(
                    r.registry_root,
                    r.owner,
                    r.repo,
                    r.lane,
                    r.branch,
                    r.landing_branch,
                    r.title,
                    r.body_path,
                    path_export=r.path_export,
                    bus_lane=r.bus_lane,
                )
            }
        elif action == "render_bot_pr":
            assert r.ended is not None
            title, text = ses.cloud_bot_pr_text(
                r.ticket,
                r.lane,
                r.session["url"],
                r.account,
                r.run_id,
                r.branch,
                r.ended,
            )
            return {"title": title, "text": text}
        elif action == "render_result":
            return {
                "text": ses.cloud_result_text(
                    r.owner,
                    r.account,
                    r.session,
                    r.prs,
                    r.run_id,
                    r.ended,
                    session_branches=r.session_branches,
                    opened=r.opened,
                )
            }
        elif action == "parse_start":
            return {
                "parsed": {
                    "session": acc.parse_cloud_start(r.text, r.session_url_base),
                    "refused": acc.cloud_start_refused(r.text),
                }
            }
        elif action == "parse_session":
            parsed = ses.parse_cloud_session(r.text)
            return {"error": parsed} if isinstance(parsed, str) else {"parsed": parsed}
        elif action == "parse_login":
            return {"text": acc.cloud_login(r.text)}
        elif action == "parse_pr_url":
            assert r.repo is not None
            return {"text": ses.cloud_pr_url(r.text, r.owner, r.repo)}
        elif action == "session_branches":
            assert r.state is not None
            return {
                "branches": ses.cloud_session_branches(
                    r.state, r.landing_branch, r.pr_heads
                )
            }
        return {}
