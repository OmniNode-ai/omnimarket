# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge whether each declared install emitted a session start, a tool use and a stop (OMN-26658).

Pure. An event counts for an install when its host (case-insensitive) is the install's host or one of its aliases, its
`actor` is the install's tool, and it is at or after `since`. An install with every required kind is `emitting`, with
some `partial`, with none `silent`. An event no declared install explains (another host, or an actor of `unknown`) is
counted in `undeclared` with its host and actor, never dropped, so a machine missing from the declaration is visible.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from ..models import (
    ModelHookInstallAlert,
    ModelHookInstallDeclared,
    ModelHookInstallEmissionRequest,
    ModelHookInstallEmissionResult,
    ModelHookInstallObservedEvent,
    ModelHookInstallUndeclared,
    ModelHookInstallVerdict,
)


def _names(install: ModelHookInstallDeclared) -> frozenset[str]:
    return frozenset(n.casefold() for n in (install.host, *install.host_aliases))


class HandlerHookInstallEmission:
    """Pure: the same request gives the same result."""

    def handle(
        self, request: ModelHookInstallEmissionRequest
    ) -> ModelHookInstallEmissionResult:
        names = {(i.host, i.tool): _names(i) for i in request.installs}
        owned: dict[tuple[str, str], list[ModelHookInstallObservedEvent]] = {
            key: [] for key in names
        }
        unexplained: Counter[tuple[str, str]] = Counter()
        for event in request.events:
            if event.emitted_at < request.since:
                continue
            for (host, tool), host_names in names.items():
                if event.actor == tool and event.host.casefold() in host_names:
                    owned[(host, tool)].append(event)
                    break
            else:
                unexplained[(event.host, event.actor)] += 1

        verdicts = tuple(
            self._verdict(install, owned[(install.host, install.tool)], request)
            for install in request.installs
        )
        silent = tuple(v for v in verdicts if v.status == "silent")
        undeclared = tuple(
            ModelHookInstallUndeclared(host=host, actor=actor, event_count=count)
            for (host, actor), count in sorted(unexplained.items())
        )
        alerts = self._alerts(request, owned)
        return ModelHookInstallEmissionResult(
            alerts=alerts,
            alert_text="; ".join(
                f"SILENT INSTALL {a.host} {a.tool}: session at {a.session_at:%Y-%m-%dT%H:%M:%SZ}, "
                + (
                    "no hook event since"
                    if a.newest_event_at is None
                    else f"newest hook event {a.newest_event_at:%Y-%m-%dT%H:%M:%SZ}"
                )
                for a in alerts
            ),
            installs=verdicts,
            silent_installs=silent,
            undeclared=undeclared,
            all_emitting=all(v.status == "emitting" for v in verdicts),
            read_at=request.read_at,
        )

    @staticmethod
    def _alerts(
        request: ModelHookInstallEmissionRequest,
        owned: dict[tuple[str, str], list[ModelHookInstallObservedEvent]],
    ) -> tuple[ModelHookInstallAlert, ...]:
        """One alert per install whose last session is `quiet_minutes` old with no hook event at or after it."""
        quiet = timedelta(minutes=request.quiet_minutes)
        out: list[ModelHookInstallAlert] = []
        for install in request.installs:
            session = install.last_session_at
            if session is None or request.read_at - session < quiet:
                continue
            newest = max(
                (e.emitted_at for e in owned[(install.host, install.tool)]),
                default=None,
            )
            if newest is not None and newest >= session:
                continue
            out.append(
                ModelHookInstallAlert(
                    host=install.host,
                    tool=install.tool,
                    session_at=session,
                    newest_event_at=newest,
                )
            )
        return tuple(out)

    @staticmethod
    def _verdict(
        install: ModelHookInstallDeclared,
        events: list[ModelHookInstallObservedEvent],
        request: ModelHookInstallEmissionRequest,
    ) -> ModelHookInstallVerdict:
        seen_names = {e.hook_event_name for e in events}
        seen = tuple(
            k.kind
            for k in request.required_kinds
            if seen_names & set(k.hook_event_names)
        )
        missing = tuple(k.kind for k in request.required_kinds if k.kind not in seen)
        status = "emitting" if not missing else ("silent" if not seen else "partial")
        return ModelHookInstallVerdict(
            host=install.host,
            tool=install.tool,
            status=status,
            kinds_seen=seen,
            kinds_missing=missing,
            event_count=len(events),
            newest_event_at=max((e.emitted_at for e in events), default=None),
        )
