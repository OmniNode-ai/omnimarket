# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the quarantine suite: every test runs on the in-memory rig and on the real files and git rig."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_code_quarantine_effect.handlers.handler_code_quarantine import (
    HandlerCodeQuarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.tests.support import (
    Rig,
    RigFactory,
    files_rig,
    memory_rig,
)


@pytest.fixture(params=["in-memory", "files"])
def make_rig(request: pytest.FixtureRequest, tmp_path: Path) -> RigFactory:
    if request.param == "in-memory":
        return memory_rig
    return lambda cls: files_rig(tmp_path, cls)


@pytest.fixture
def rig(make_rig: RigFactory) -> Rig:
    return make_rig(HandlerCodeQuarantine)
