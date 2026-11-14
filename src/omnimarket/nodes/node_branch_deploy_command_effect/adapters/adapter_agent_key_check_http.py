# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent key check http adapter of node_branch_deploy_command_effect."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_agent_key_check_reading import (
    ModelAgentKeyCheckReading,
)


def fetch_health(url: str, timeout_seconds: float) -> bytes:
    """The one http boundary of this adapter; tests replace it."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        return exc.read()


class AgentKeyCheckHttpReader:
    """Reads the agent key check over one http call."""

    def __init__(
        self,
        url: str,
        timeout_seconds: float,
        field: str,
        fetch: Callable[[str, float], bytes] | None = None,
    ) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._field = field
        self._fetch = fetch

    def read(self) -> ModelAgentKeyCheckReading:
        try:
            body = (self._fetch or fetch_health)(self._url, self._timeout_seconds)
            document = json.loads(body)
            if not isinstance(document, dict):
                return ModelAgentKeyCheckReading(status="unreachable")
            value = document.get(self._field)
            if isinstance(value, str) and value:
                return ModelAgentKeyCheckReading(status="read", key_check=value)
            return ModelAgentKeyCheckReading(status="absent")
        except Exception as exc:
            return ModelAgentKeyCheckReading(
                status="unreachable", detail=type(exc).__name__
            )
