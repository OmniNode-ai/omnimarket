# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live per-team probe for the demotion ratchet (OMN-16536 AC#2).

Reads every team's ``gitAutomationStates`` back from the Linear GraphQL API in
two phases and runs the pure :mod:`git_automation_demotion_guard` assertion over
the result.

Why two phases instead of one nested query
------------------------------------------
The OMN-15373 sibling uses a single nested
``teams { gitAutomationStates { state } }`` query. That query's complexity is the
**product** of its two page sizes, and it breaches Linear's 10000 budget as soon
as both are opened up — measured 2026-08-27:

    teams(50) x gitAutomationStates(50)  -> HTTP 400, complexity 11565  (REJECTED)
    teams(50) x gitAutomationStates(10)  -> OK
    teams(25) x gitAutomationStates(25)  -> OK

To stay inside the cap the nested form has to hold automations at ``first: 10``,
which silently truncates any team carrying 11 — and the nested query gives no
place to check for that. Splitting the read fixes both problems at once:

1. Phase one enumerates teams only (cheap, flat).
2. Phase two reads ONE team's automations per query, at the full ``first: 50``.

Each phase-two query's complexity is constant regardless of how many teams the
workspace grows to, and the returned page size is checked against the request so
possible truncation fails closed instead of hiding an automation.

Phase one doubles as the ratchet's positive control. The steady-state green here
is an empty automation set on every team (AC#1 deleted all ten mappings on
2026-08-27), and an empty set is worthless as evidence unless the probe is
independently proven to have run. Enumerating teams first proves the token is
live and scoped to the workspace before any emptiness is believed.

Read-only: this handler never mutates a Linear setting. Correcting a re-added
automation is an operator action — teams CON and JON in particular are owned
elsewhere, and a guard that silently rewrote another team's workspace
configuration would be a worse failure than the one it detects.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_linear_triage.models.model_git_automation_demotion_guard import (
    ModelDemotionAuditReport,
    ModelTeamAutomationProbe,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationState,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_demotion_guard import (
    audit_demotion_risk,
)

_log = logging.getLogger(__name__)

__all__ = [
    "AUTOMATION_PAGE_SIZE",
    "HandlerGitAutomationDemotionGuard",
    "LinearGraphQLPerTeamTransport",
    "LinearPerTeamTransport",
    "parse_team_automations",
]

# Full page. Affordable only because phase two reads one team at a time; the
# nested org-wide form is capped at 10 by Linear's complexity budget.
AUTOMATION_PAGE_SIZE = 50
TEAM_PAGE_SIZE = 100

# `pageInfo` is not decoration. Without it a workspace holding more than
# TEAM_PAGE_SIZE teams would enumerate only the first page, and the teams beyond
# it would never be probed — while the report still read passed=True with the
# positive control satisfied. That is the one fail-open path this design cannot
# tolerate, and it is the same truncation hazard already rejected for the
# automation page.
_TEAMS_QUERY = f"""
{{
  teams(first: {TEAM_PAGE_SIZE}) {{
    nodes {{ id key name }}
    pageInfo {{ hasNextPage }}
  }}
}}
"""

_TEAM_AUTOMATIONS_QUERY = f"""
query($teamId: String!) {{
  team(id: $teamId) {{
    id
    key
    gitAutomationStates(first: {AUTOMATION_PAGE_SIZE}) {{
      nodes {{
        id
        event
        state {{ id name type }}
        targetBranch {{ id branchPattern }}
      }}
    }}
  }}
}}
"""

# Errors that mean "the probe did not work" rather than "the code is wrong".
# Anything outside this set is a bug and is allowed to propagate.
_PROBE_ERRORS = (
    RuntimeError,
    OSError,
    urllib.error.URLError,
    json.JSONDecodeError,
    ValueError,
    KeyError,
    TypeError,
)


@runtime_checkable
class LinearPerTeamTransport(Protocol):
    """Injectable probe boundary — the two phases, decoupled from transport."""

    def enumerate_teams(self) -> list[dict[str, str]]:
        """Return ``[{id, key, name}, ...]`` for every team in the workspace.

        Implementations MUST fail closed rather than return a truncated page —
        an unenumerated team is never probed, so its automations would read as
        clean.
        """

    def fetch_team_automations(self, team_id: str) -> list[dict[str, object]]:
        """Return one team's raw ``gitAutomationStates`` nodes."""


class LinearGraphQLPerTeamTransport:
    """Live two-phase transport against the Linear GraphQL API.

    The only networked class in this module. A GraphQL ``errors`` block is
    raised rather than returned, so a partial or error response can never be
    mistaken for an empty-and-therefore-clean result.
    """

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            LINEAR_GRAPHQL_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body: Any = json.loads(resp.read())
        if not isinstance(body, dict):
            raise RuntimeError(
                f"unexpected Linear response type: {type(body).__name__}"
            )
        if body.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {body['errors']}")
        return body

    def enumerate_teams(self) -> list[dict[str, str]]:
        body = self._post(_TEAMS_QUERY)
        teams_block = (body.get("data") or {}).get("teams") or {}
        nodes = teams_block.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError(
                "Linear team enumeration returned no 'teams.nodes' list — the query "
                "shape may have changed. Failing closed rather than reading it as "
                "zero teams."
            )

        # A team beyond the page boundary would never be probed, yet the report
        # would still read passed=True. Fail closed on both signals: the
        # authoritative cursor, and a page that came back exactly full (which
        # cannot prove it was the last page even if pageInfo went missing).
        page_info = teams_block.get("pageInfo")
        has_next = (
            bool(page_info.get("hasNextPage")) if isinstance(page_info, dict) else False
        )
        if has_next or len(nodes) >= TEAM_PAGE_SIZE:
            raise RuntimeError(
                f"Linear returned {len(nodes)} team(s) at first: {TEAM_PAGE_SIZE} with "
                f"hasNextPage={has_next}. Teams beyond the page would be invisible to "
                "this audit, so a demoting automation on one of them would read as "
                "clean. Failing closed on possible team-page truncation."
            )
        out: list[dict[str, str]] = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            out.append(
                {
                    "id": str(n.get("id") or ""),
                    "key": str(n.get("key") or n.get("name") or "?"),
                    "name": str(n.get("name") or ""),
                }
            )
        return out

    def fetch_team_automations(self, team_id: str) -> list[dict[str, object]]:
        body = self._post(_TEAM_AUTOMATIONS_QUERY, {"teamId": team_id})
        team = (body.get("data") or {}).get("team")
        if not isinstance(team, dict):
            raise RuntimeError(
                f"Linear returned no 'team' block for team id {team_id!r}. Failing "
                "closed rather than reading it as zero automations."
            )
        nodes = (team.get("gitAutomationStates") or {}).get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError(
                f"Linear returned no 'gitAutomationStates.nodes' list for team "
                f"{team_id!r} — the query shape may have changed."
            )
        return [n for n in nodes if isinstance(n, dict)]


def parse_team_automations(
    team_key: str, team_id: str, nodes: list[dict[str, object]]
) -> list[ModelGitAutomationState]:
    """Map one team's raw ``gitAutomationStates`` nodes onto the shared model.

    Fail-closed on shape: an automation whose ``state`` block is missing or
    malformed is emitted with ``state_readable=False`` rather than dropped or
    defaulted to a benign type. Dropping it would silently shrink the audit
    population, which is the same failure as not checking at all.
    """
    out: list[ModelGitAutomationState] = []
    for node in nodes:
        raw_state = node.get("state")
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        readable = bool(state.get("type"))
        branch = node.get("targetBranch")
        pattern = (
            str(branch.get("branchPattern") or "") if isinstance(branch, dict) else ""
        )
        out.append(
            ModelGitAutomationState(
                team_key=team_key,
                team_id=team_id,
                automation_id=str(node.get("id") or ""),
                event=str(node.get("event") or "?"),
                state_id=str(state.get("id") or "") if readable else "",
                state_name=str(state.get("name") or "") if readable else "",
                state_type=str(state.get("type") or "") if readable else "",
                state_readable=readable,
                target_branch=pattern or None,
            )
        )
    return out


class HandlerGitAutomationDemotionGuard:
    """Probe Linear per team and return the fail-closed demotion verdict."""

    def __init__(self, transport: LinearPerTeamTransport | None = None) -> None:
        self._transport = transport

    def _get_transport(self) -> LinearPerTeamTransport:
        if self._transport is not None:
            return self._transport
        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY environment variable is not set. Export it before "
                "running the git-automation demotion ratchet."
            )
        return LinearGraphQLPerTeamTransport(api_key)

    def handle(self, *, now: datetime) -> ModelDemotionAuditReport:
        """Run the two-phase live probe and audit.

        Never raises on probe failure — it returns a FAILING report, so the
        scheduled runner's exit path is uniform and an API error can never be
        mistaken for an infrastructure flake that "didn't really fail".
        """
        # Phase one: enumerate teams. This is the positive control.
        try:
            transport = self._get_transport()
            teams = transport.enumerate_teams()
        except _PROBE_ERRORS as exc:
            _log.error("Linear team enumeration failed: %s", exc)
            return audit_demotion_risk(
                [],
                teams_enumerated=0,
                enumeration_ok=False,
                enumeration_error=str(exc),
                now=now,
            )

        # Phase two: one query per team, so complexity stays constant and the
        # automation page can be requested at full width.
        probes: list[ModelTeamAutomationProbe] = []
        for team in teams:
            team_id = team.get("id", "")
            team_key = team.get("key", "?")
            try:
                nodes = transport.fetch_team_automations(team_id)
            except _PROBE_ERRORS as exc:
                _log.error("git-automation probe failed for team %s: %s", team_key, exc)
                probes.append(
                    ModelTeamAutomationProbe(
                        team_key=team_key,
                        team_id=team_id,
                        probe_ok=False,
                        probe_error=str(exc),
                        automations=[],
                        page_size=AUTOMATION_PAGE_SIZE,
                    )
                )
                continue

            probes.append(
                ModelTeamAutomationProbe(
                    team_key=team_key,
                    team_id=team_id,
                    probe_ok=True,
                    automations=parse_team_automations(team_key, team_id, nodes),
                    page_size=AUTOMATION_PAGE_SIZE,
                )
            )

        return audit_demotion_risk(
            probes,
            teams_enumerated=len(teams),
            enumeration_ok=True,
            now=now,
        )
