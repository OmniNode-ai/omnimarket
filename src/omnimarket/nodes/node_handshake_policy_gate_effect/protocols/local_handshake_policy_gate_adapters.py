# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local adapters of the policy gate ports: the GitHub REST reader and a real sleep (OMN-20671).

The token is resolved at this boundary from the contract-declared secret, never read from the
process environment by name here. It is resolved at the first read, never when the reader is
built: the runtime builds the handler at kernel boot, where a missing secret would stop the
kernel instead of failing one run (OMN-20871).
"""

from __future__ import annotations

import time
from http import HTTPStatus
from pathlib import Path

from omnimarket.github_api import GitHubApiError, rest_json
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_handshake_policy_gate_effect.protocols.protocol_handshake_policy_gate_run import (
    PolicyGatePortError,
    PolicyGateRead,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_SECRET_NAME = "GH_TOKEN"


def resolve_policy_gate_token() -> str:
    """The GitHub token the contract declares, resolved through the secret store."""
    ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
    secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
    if secret is None:
        raise PolicyGatePortError(
            f"no GitHub token: {_SECRET_NAME} is not set in the secret store"
        )
    token = secret.get_secret_value()
    if not token:
        raise PolicyGatePortError("GitHub token must not be empty")
    return token


def _error_text(exc: GitHubApiError) -> str:
    """Status line of a failed read; a body is never echoed, so it cannot mimic a 404."""
    if exc.status_code is None:
        return str(exc)
    try:
        reason = HTTPStatus(exc.status_code).phrase
    except ValueError:
        reason = "Unknown"
    return f"HTTP {exc.status_code}: {reason}"


class GitHubPolicyGateReader:
    """Reads the repo and workflow-runs endpoints through the shared GitHub REST helper."""

    def __init__(self, token: str | None = None) -> None:
        if token is not None and not token:
            raise PolicyGatePortError("GitHub token must not be empty")
        self._token = token

    def _resolved_token(self) -> str:
        if self._token is None:
            self._token = resolve_policy_gate_token()
        return self._token

    def default_branch(self, endpoint: str) -> str:
        """The default branch, or "" when the lookup fails: the compute node then names "main"."""
        try:
            page = rest_json("GET", f"/{endpoint}", token=self._resolved_token())
        except GitHubApiError:
            return ""
        branch = page.get("default_branch")
        return branch if isinstance(branch, str) else ""

    def latest_run(self, endpoint: str) -> PolicyGateRead:
        try:
            page = rest_json("GET", f"/{endpoint}", token=self._resolved_token())
        except GitHubApiError as exc:
            return PolicyGateRead(api_ok=False, api_error_text=_error_text(exc))
        runs = page.get("workflow_runs")
        first = runs[0] if isinstance(runs, list) and runs else {}
        conclusion = first.get("conclusion") if isinstance(first, dict) else None
        total = page.get("total_count")
        return PolicyGateRead(
            api_ok=True,
            total_count=total if isinstance(total, int) else 0,
            conclusion=conclusion if isinstance(conclusion, str) else "",
        )


class TimePolicyGateSleeper:
    def sleep(self, seconds: int) -> None:
        time.sleep(seconds)
