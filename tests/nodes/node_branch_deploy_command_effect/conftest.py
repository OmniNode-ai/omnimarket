# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory sees a deploy agent that holds the fixture signing key (OMN-24396).

The handler reads the agent's health page before it publishes. This fixture replaces the one http boundary of that read,
so no test reaches the network (a module that sets REAL_HTTP_BOUNDARY keeps the real one); a test about the key check builds its own reader or replaces the boundary again.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.adapters import (
    adapter_agent_key_check_http,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    key_check_value,
)
from tests.nodes.node_branch_deploy_command_effect.support import SIGNING_KEY


@pytest.fixture(autouse=True)
def _agent_holds_the_fixture_key(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if getattr(request.module, "REAL_HTTP_BOUNDARY", False):
        return  # the adapter's own tests read a real local socket
    body = json.dumps({"hmac_key_check": key_check_value(SIGNING_KEY)}).encode()
    monkeypatch.setattr(
        adapter_agent_key_check_http, "fetch_health", lambda *_args: body
    )
