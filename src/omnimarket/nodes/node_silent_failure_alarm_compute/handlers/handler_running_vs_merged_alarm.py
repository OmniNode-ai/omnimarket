# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide which hosts run something other than merged main, for over an hour (OMN-19998).

THE INCIDENT. On 2026-10-15 a pulse fix merged to the internal plugin repository hours before it ran anywhere.
The host-drift fleet fold already said why: h101's omni plugin sat at 0.4.406 with 0.4.420 merged
for 72 hours, the dispatch venvs on h101, h201 and h202 were five to seven hours behind omnimarket,
h200's plugin tree carried eight hot-patched files and the landing controller ran a branch install.
Every one was an open item in a file on the operator host, and none of it reached the tick.

WHAT IS DECIDED. Each open item of the fold whose kind is something a merge deploys (the omni
plugin, a dispatch venv, an installed controller, an installed launchd lib) and that has been open
for longer than ``bound_minutes`` (60) is one MISSING finding, named
``running-vs-merged:<surface>``, with the command that brings that surface to main. Clones, disks,
units and plist content are host drift too, but not merged code a deploy carries, so host-drift's
own alerts own them. A launchd-plist item counts only when it names installed files that differ
from the checkout; a diff of the plist's own content is configuration.

A fold that cannot be read, or one older than ``fleet_max_age_minutes`` (45, three missed
fifteen-minute checks), is UNKNOWN, never OK: the alarm that checks deploys must not pass because
the reader that sees them stopped.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from ..models import (
    ModelRunningSurface,
    ModelRunningVsMergedFinding,
    ModelRunningVsMergedRequest,
    ModelRunningVsMergedResult,
)

LOOP = "running-vs-merged"
# The installed files of a launchd lib differ from the canonical checkout; anything else on a
# launchd-plist item (a "@@" diff) is the plist's own content.
LIB_MARKERS = ("differs: ", "missing from installed: ", "only in installed: ")
ADOPT = (
    'python3 "$(ls -d ~/.claude/plugins/cache/omninode-internal/omni/*/skills/adopt-merged'
    ' | sort -V | tail -1)/scripts/adopt_merged.py"'
)
FLEET_FIX = (
    "read the host-drift run on the operator host: tail ~/.omninode/host-drift/launchd.out.log "
    "and launchctl list ai.omninode.host-drift; its fold is "
    "$OMNI_HOME/.onex_state/host-drift/fleet.json"
)

__all__ = [
    "HandlerRunningVsMergedAlarm",
    "ModelRunningVsMergedResult",
    "request_from_fleet",
]


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days * 24 + hours}h{mins:02d}m"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _surface(item: ModelRunningSurface) -> tuple[str, str] | None:
    """The surface name and its fix, or None when the item is not merged code a deploy carries."""
    host = item.host
    _, _, name = item.key.partition(":")
    if item.kind == "plugin":
        if "hotpatch" in item.observed:
            return (
                f"plugin:{host}",
                f"on {host}: {ADOPT} backups --since {_stamp(item.first_seen_at)}, land or drop "
                "each hot-patched file, then the host-drift repair-plugin action reinstalls the "
                "shipped revision",
            )
        return (
            f"plugin:{host}",
            f"on {host}: claude plugin marketplace update omninode-internal && "
            f"claude plugin update {name} (the host-drift update-plugin action; read why it has "
            "not run in the sync hub's state)",
        )
    if item.kind == "dispatch-venv":
        return (
            f"dispatch-venv:{host}:{name}",
            f'on {host}: bash "$OMNI_HOME/omnibase_infra/scripts/reconcile-workspace-venvs.sh" '
            f"for the root holding {name} (the host-drift update-venv action; read its last fix "
            "outcome in the sync hub's state)",
        )
    if item.kind == "controller":
        return (
            f"controller:{host}:{name}",
            f"on {host}: move the newest "
            "~/.omninode/landing-controller/landing_install.json.moved-aside.* back to "
            "landing_install.json; the next controller tick refreshes from origin/main through its "
            "own install path (landing_g_act.refresh_install: prove-install receipts, then "
            "install.sh --refresh-install)",
        )
    if item.kind == "launchd-plist" and any(
        marker in item.observed for marker in LIB_MARKERS
    ):
        return (
            f"launchd-lib:{host}:{name}",
            f"on {host}: land or drop any hot-patched file, then rerun the installer of {name} "
            f"from the internal tooling canonical clone at origin/main ({ADOPT} run reruns the "
            "installers it lists)",
        )
    return None


class HandlerRunningVsMergedAlarm:
    """Which surfaces run something other than merged main past the bound."""

    def handle(
        self, request: ModelRunningVsMergedRequest
    ) -> ModelRunningVsMergedResult:
        """Return OK, MISSING with one finding per surface, or UNKNOWN."""
        if not request.fleet_readable:
            return self._unknown(
                f"host-drift fleet fold unreadable: {request.unreadable_why or 'no reason given'}"
            )
        if request.folded_at is None:
            return self._unknown("host-drift fleet fold names no folded_at")
        stale = request.now - request.folded_at
        if stale > timedelta(minutes=request.fleet_max_age_minutes):
            return self._unknown(
                f"host-drift fleet fold is {_age(stale)} old (folded {_stamp(request.folded_at)}), "
                f"over {request.fleet_max_age_minutes}m"
            )
        bound = timedelta(minutes=request.bound_minutes)
        findings: dict[str, ModelRunningVsMergedFinding] = {}
        for item in request.items:
            age = request.now - item.first_seen_at
            if age <= bound:
                continue
            surface = _surface(item)
            if surface is None:
                continue
            name, fix = surface
            loop = f"{LOOP}:{name}"
            why = (
                f"{item.host} {item.key} differs from merged main for {_age(age)} "
                f"(since {_stamp(item.first_seen_at)}): {item.verdict} {item.observed}"
            )
            findings.setdefault(
                loop,
                ModelRunningVsMergedFinding(
                    level="missing", loop=loop, why=why, fix=fix
                ),
            )
        if not findings:
            return ModelRunningVsMergedResult(state="OK")
        return ModelRunningVsMergedResult(
            state="MISSING", findings=tuple(findings[loop] for loop in sorted(findings))
        )

    @staticmethod
    def _unknown(why: str) -> ModelRunningVsMergedResult:
        return ModelRunningVsMergedResult(
            state="UNKNOWN",
            findings=(
                ModelRunningVsMergedFinding(
                    level="unknown", loop=LOOP, why=why, fix=FLEET_FIX
                ),
            ),
        )


def request_from_fleet(
    fleet: Mapping[str, object], now: datetime, *, bound_minutes: int = 60
) -> ModelRunningVsMergedRequest:
    """The request for a host-drift fleet fold payload (fleet.json).

    A malformed fold is unreadable."""
    try:
        hosts = fleet.get("hosts")
        if not isinstance(hosts, list):
            raise TypeError("hosts is not a list")
        items: list[ModelRunningSurface] = []
        for host in hosts:
            if not isinstance(host, Mapping):
                raise TypeError("a host is not an object")
            for item in host.get("open") or ():
                if not isinstance(item, Mapping):
                    raise TypeError("an open item is not an object")
                items.append(
                    ModelRunningSurface(
                        host=str(host["host"]),
                        key=str(item["key"]),
                        kind=str(item["kind"]),
                        observed=str(item.get("observed") or ""),
                        first_seen_at=item["first_seen_at"],
                        verdict=str(item.get("verdict") or "DRIFT"),
                    )
                )
        return ModelRunningVsMergedRequest.model_validate(
            {
                "now": now,
                "folded_at": fleet.get("folded_at"),
                "items": tuple(items),
                "bound_minutes": bound_minutes,
            }
        )
    except (KeyError, TypeError, ValidationError) as exc:
        why = " ".join(str(exc).split())[:200]
        return ModelRunningVsMergedRequest(
            now=now, fleet_readable=False, unreadable_why=f"malformed: {why}"
        )
