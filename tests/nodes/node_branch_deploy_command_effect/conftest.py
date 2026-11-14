# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test in this directory sees a deploy agent that holds the fixture signing key (OMN-24396, OMN-24413).

The handler asks node_deploy_agent_key_check_effect for the deploy agent's key check value before it publishes. This
fixture gives that node an overlay root and the fixture key in the environment, so no test needs a deployment's overlay;
a test about the key check builds its own reader or replaces the environment again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes.node_branch_deploy_command_effect.support import SIGNING_KEY
from tests.nodes.node_deploy_agent_key_check_effect.support import (
    AGENT_KEY_ENV,
)
from tests.nodes.node_deploy_agent_key_check_effect.support import (
    NODE_DIR as KEY_CHECK_NODE_DIR,
)


@pytest.fixture(autouse=True)
def _agent_holds_the_fixture_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = tmp_path / "node_deploy_agent_key_check_effect"
    node.mkdir()
    (node / "overlay.yaml").write_text(
        (KEY_CHECK_NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv(AGENT_KEY_ENV, SIGNING_KEY)
