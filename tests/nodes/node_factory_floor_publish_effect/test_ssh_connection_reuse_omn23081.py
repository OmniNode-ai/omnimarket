# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23081: every ssh call of a publish cycle shares one connection.

A serving host busy with disk work took 1.7 to 5 s to open each connection. The cycle opens one for every copy,
listing and removal (20 or more), so the handshakes spent the 40 s copy budget and the page stopped moving.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
    ssh_argv,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS

pytestmark = pytest.mark.unit

RT = load_runtime().with_limits(TEST_LIMITS)


def _options(argv: list[str]) -> set[str]:
    return {argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "-o"}


def test_ssh_calls_share_one_control_connection() -> None:
    overlay = ModelFactoryFloorPublishOverlay.model_construct(ssh_target="host")
    argv = ssh_argv(RT, overlay, "true")
    options = _options(argv)
    assert "ControlMaster=auto" in options
    assert "ControlPath=~/.ssh/ffp-%C" in options
    assert any(option.startswith("ControlPersist=") for option in options)
    assert "BatchMode=yes" in options


def test_the_connection_and_the_command_are_unchanged() -> None:
    overlay = ModelFactoryFloorPublishOverlay.model_construct(ssh_target="host")
    argv = ssh_argv(RT, overlay, "ls -1 -- /srv")
    assert argv[-2:] == ["host", "ls -1 -- /srv"]
    assert argv[argv.index("--") - 1].startswith("ControlPersist=")
