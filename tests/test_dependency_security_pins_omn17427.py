# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the transitive pins outside the ranges reported by the security scan."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from packaging.version import Version

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[1] / "uv.lock"


@pytest.mark.parametrize(
    ("package", "floor"),
    [("multidict", "6.9.1"), ("mako", "1.4.2")],
)
def test_lock_excludes_reported_vulnerabilities(package: str, floor: str) -> None:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == package]
    assert versions, f"uv.lock carries no {package} entry"
    assert all(Version(version) >= Version(floor) for version in versions), versions


def test_alembic_renders_a_revision_with_the_locked_mako(tmp_path: Path) -> None:
    config = Config(str(tmp_path / "alembic.ini"))
    command.init(config, str(tmp_path / "migrations"))
    revision = command.revision(config, message="security pin smoke")
    assert revision is not None
    assert not isinstance(revision, list)
    source = Path(revision.path).read_text(encoding="utf-8")
    module = ast.parse(source)
    functions = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    assert {"upgrade", "downgrade"} <= functions
    assert "security pin smoke" in source
