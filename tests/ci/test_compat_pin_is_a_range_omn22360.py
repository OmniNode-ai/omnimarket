# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnibase-compat is pinned as a version range, never an exact pin (OMN-22360).

The core dependency-pin policy (omnibase_core contracts/dependency_pin_policy.yaml)
wants every first-party edge as a range; an exact pin freezes this repository
below every later compat release. The core validator refuses it once this
repository wires it; this test holds the manifest until then.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION = "omnibase-compat"
EXACT = ("==", "===")


def _compat_requirements() -> list[Requirement]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tables = [
        document.get("project", {}).get("dependencies", []),
        document.get("tool", {}).get("uv", {}).get("override-dependencies", []),
    ]
    requirements = [Requirement(raw) for table in tables for raw in table]
    return [r for r in requirements if r.name == DISTRIBUTION]


def test_compat_is_declared_in_both_tables() -> None:
    assert len(_compat_requirements()) == 2


def test_compat_pin_is_a_range() -> None:
    for requirement in _compat_requirements():
        operators = {spec.operator for spec in requirement.specifier}
        assert not operators & set(EXACT), str(requirement)
