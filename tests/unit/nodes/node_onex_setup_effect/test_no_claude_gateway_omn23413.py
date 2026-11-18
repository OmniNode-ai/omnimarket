# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC37 (OMN-23413 Amendment 11): no Claude gateway remains.

Claude Code delegation is the one prompt hook (Amendment 14a); the gateway (a
local proxy Claude Code was pointed at) was removed on 2026-11-14. Failure modes:
  G1  a gateway identifier comes back in omnimarket's source, code or config.
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
_SCANNED = {".py", ".yaml", ".yml", ".json", ".toml"}


def _gateway_hits(root: Path) -> list[str]:
    """Every line naming the gateway in code or config under ``root``, outside test folders."""
    return [
        f"{path.relative_to(root)}:{number}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in _SCANNED
        and "tests" not in path.relative_to(root).parts
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
        if _GATEWAY.search(line)
    ]


def test_no_gateway_identifier_in_the_source() -> None:
    """G1."""
    assert _gateway_hits(_ROOT / "src" / "omnimarket") == []


def test_the_scan_finds_a_planted_gateway_name(tmp_path: Path) -> None:
    """G1 positive control: a name planted in config or code is found; one under a tests folder is not."""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "routes.yaml").write_text("backend: claude_gateway\n")
    (tmp_path / "proxy.py").write_text("class ClaudeGateway: ...\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_left.py").write_text("ClaudeGateway\n")
    assert _gateway_hits(tmp_path) == ["configs/routes.yaml:1", "proxy.py:1"]


def test_onex_has_no_claude_command_group() -> None:
    """G2."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    groups = project["project"]["entry-points"]["onex.cli"]
    assert "delegation" in groups  # the control: the table is the one onex reads
    assert "claude" not in groups
