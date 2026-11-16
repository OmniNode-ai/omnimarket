# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC37 (OMN-23413 Amendment 11): no Claude gateway remains.

Claude Code delegation is the two hooks; the gateway (a local proxy Claude Code
was pointed at) was removed on 2026-11-14. Failure modes:
  G1  a gateway identifier comes back in omnimarket's source.
  G2  `onex claude` is registered again as an onex command group.
Tests may name the gateway, to prove a leftover of it is ignored.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_GATEWAY = re.compile(r"claude_gateway|cli_gateway|gateway_owns_session|ClaudeGateway")


def test_no_gateway_identifier_in_the_source() -> None:
    """G1."""
    found = [
        f"{path.relative_to(_ROOT)}:{number}"
        for path in sorted((_ROOT / "src" / "omnimarket").rglob("*.py"))
        if "tests" not in path.relative_to(_ROOT).parts
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _GATEWAY.search(line)
    ]
    assert found == []


def test_onex_has_no_claude_command_group() -> None:
    """G2."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    groups = project["project"]["entry-points"]["onex.cli"]
    assert "delegation" in groups  # the control: the table is the one onex reads
    assert "claude" not in groups
