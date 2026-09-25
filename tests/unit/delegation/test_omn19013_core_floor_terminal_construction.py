# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19013: the Core floor admits only a release carrying the construction pair.

omnibase-core 0.47.22 is the first published release carrying core#1737: the
``TERMINAL_CONSTRUCTION_FAILED`` operational outcome and the ``UNDETERMINED``
content verdict. Migration 0045 in this repo filters on exactly that pair, and
omnibase_infra vendors 0045 from here, so a resolve below the floor would ship
a projection filter for values no installed terminal model can carry.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[3]
_PAIR_RELEASE = Version("0.47.22")


def _core_requirements() -> list[Requirement]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [
        *data["project"]["dependencies"],
        *data["tool"]["uv"]["override-dependencies"],
    ]
    return [
        Requirement(spec)
        for spec in declared
        if Requirement(spec).name == "omnibase-core"
    ]


@pytest.mark.unit
def test_core_floor_excludes_every_release_without_the_pair() -> None:
    requirements = _core_requirements()
    # Both places, in lockstep: the override decides transitive resolves.
    assert len(requirements) == 2
    for requirement in requirements:
        assert not requirement.specifier.contains("0.47.21"), str(requirement)
        # The floor may rise past the pair release (OMN-19531 raised it to
        # 0.47.23); what must hold is that it never drops below it.
        floors = [
            Version(spec.version)
            for spec in requirement.specifier
            if spec.operator == ">="
        ]
        assert floors, str(requirement)
        assert min(floors) >= _PAIR_RELEASE, str(requirement)


@pytest.mark.unit
def test_installed_core_carries_the_construction_pair() -> None:
    from omnibase_core.enums.enum_delegation_content_verdict import (
        EnumDelegationContentVerdict,
    )
    from omnibase_core.enums.enum_delegation_operational_outcome import (
        EnumDelegationOperationalOutcome,
    )

    assert (
        EnumDelegationOperationalOutcome.TERMINAL_CONSTRUCTION_FAILED.value
        == "terminal_construction_failed"
    )
    assert EnumDelegationContentVerdict.UNDETERMINED.value == "undetermined"


@pytest.mark.unit
def test_migration_0045_filters_on_the_core_pair_values() -> None:
    sql = (
        _ROOT
        / "src/omnimarket/nodes/node_projection_delegation/migrations"
        / "0045_terminal_construction_outcome_metrics.sql"
    ).read_text(encoding="utf-8")
    assert "('terminal_construction_failed', 'undetermined')" in sql
