# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGitAutomationGuard — live probe + fail-closed audit (OMN-15373).

Reads every team's ``gitAutomationStates`` back from the Linear GraphQL API and
runs the pure :mod:`git_automation_guard` assertion over them: no automation on
any team may resolve to a ``completed``-type workflow state, because such an
automation writes ``Done`` on PR merge with no closing keyword, no ``dod_verify``
receipt and no evidence gate of any kind.

The probe is deliberately separated from the decision so the decision stays
pure and unit-testable against recorded live shapes, and so the guard is
runnable headless on a schedule with no human in the loop.

Fail-closed at every boundary: a transport error, a GraphQL error, an
unparseable response, or an EMPTY automation set all produce ``passed=False``.
A probe that read nothing has proven nothing.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Protocol

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationAuditReport,
    ModelGitAutomationException,
    ModelGitAutomationState,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_guard import (
    audit_git_automations,
)

_log = logging.getLogger(__name__)

# Page sizes are deliberately small: Linear rejects the nested
# teams -> gitAutomationStates -> state query above a complexity budget of
# 10000, and `first: 50` on both levels measures 11815. `first: 10` on both is
# well inside the budget and far above the real cardinality (3 teams, 4
# automations each as of 2026-07-30).
_TEAM_PAGE = 10
_AUTOMATION_PAGE = 10

_QUERY = f"""
{{
  teams(first: {_TEAM_PAGE}) {{
    nodes {{
      id
      name
      key
      gitAutomationStates(first: {_AUTOMATION_PAGE}) {{
        nodes {{
          id
          event
          state {{ id name type }}
          targetBranch {{ id branchPattern }}
        }}
      }}
    }}
  }}
}}
"""


class GitAutomationProbe(Protocol):
    """Injectable probe boundary — returns the raw Linear GraphQL response."""

    def fetch(self) -> dict[str, Any]: ...


class LinearGitAutomationProbe:
    """Live probe against the Linear GraphQL API. The only networked class here."""

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def fetch(self) -> dict[str, Any]:
        payload = json.dumps({"query": _QUERY}).encode()
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
            data: dict[str, Any] = json.loads(resp.read())
        if data.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data


def parse_automations(response: dict[str, Any]) -> list[ModelGitAutomationState]:
    """Flatten a Linear ``teams { gitAutomationStates }`` response.

    Fail-closed on shape: an automation whose ``state`` block is missing or
    malformed is emitted with ``state_readable=False`` rather than being dropped
    or defaulted to a benign type. Dropping it would silently shrink the audit
    population, which is the same failure as not checking at all.
    """
    out: list[ModelGitAutomationState] = []
    teams = ((response.get("data") or {}).get("teams") or {}).get("nodes") or []
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_key = str(team.get("key") or team.get("name") or "?")
        team_id = str(team.get("id") or "")
        nodes = ((team.get("gitAutomationStates") or {}).get("nodes")) or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            raw_state = node.get("state")
            state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
            readable = bool(state.get("type"))
            branch = node.get("targetBranch")
            pattern = (
                str(branch.get("branchPattern") or "")
                if isinstance(branch, dict)
                else ""
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


class HandlerGitAutomationGuard:
    """Probe Linear and return the fail-closed git-automation drift verdict.

    Read-only: this handler never mutates a Linear setting. Correcting a drifted
    automation is an operator action (the Contractors and JonahPrivate teams in
    particular are owned elsewhere), and a guard that silently rewrites another
    team's workspace configuration would be a worse failure than the one it
    detects. The guard's job is to make drift impossible to miss.
    """

    def __init__(self, probe: GitAutomationProbe | None = None) -> None:
        self._probe = probe

    def _get_probe(self) -> GitAutomationProbe:
        if self._probe is not None:
            return self._probe
        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY environment variable is not set. "
                "Export it before running the git-automation drift guard."
            )
        return LinearGitAutomationProbe(api_key)

    def handle(
        self,
        *,
        now: datetime,
        exceptions: list[ModelGitAutomationException] | None = None,
    ) -> ModelGitAutomationAuditReport:
        """Run the live probe and audit. Never raises on probe failure — it
        returns a FAILING report, so the caller's exit path is uniform."""
        try:
            probe = self._get_probe()
            response = probe.fetch()
        except (
            RuntimeError,
            OSError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            _log.error("git-automation probe failed: %s", exc)
            return audit_git_automations(
                [], now=now, probe_ok=False, probe_error=str(exc)
            )

        automations = parse_automations(response)
        return audit_git_automations(automations, exceptions=exceptions, now=now)


__all__ = [
    "GitAutomationProbe",
    "HandlerGitAutomationGuard",
    "LinearGitAutomationProbe",
    "parse_automations",
]
