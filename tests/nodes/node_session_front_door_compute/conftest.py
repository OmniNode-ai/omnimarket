# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``machine`` fixture: a member's machine and the operator's, in a temporary directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.session_front_door_machine import Machine, build_machine


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return build_machine(tmp_path)
