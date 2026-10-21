# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: omnimarket may not declare an omnibase-core floor newer than infra pins.

Incident: omnimarket v0.4.316 declared ``omnibase-core>=0.47.46,<0.48.0`` while
omnibase_infra pinned ``omnibase-core==0.47.44`` (commit 44542e2a0, OMN-21146).
The infra runtime image installs omnimarket dev on top of the core that infra
locks, so every infra merge-queue head failed ``runtime-boot`` for about five
hours while each repository's own tests passed.

This is the producer-side check. ``INFRA_CORE_PINS`` is the list of omnibase_infra
pins this repository has recorded, spelled here so the test runs everywhere
without reading another clone. Raising the floor in ``pyproject.toml`` (in
``[project].dependencies`` and in ``[tool.uv] override-dependencies``) above the
newest recorded infra pin is refused; the order that works is core release, then
omnibase_infra pin and lock, then a row added below, then the floor here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pin_compat"
INCIDENT_REQUIREMENT = FIXTURES / "omnimarket_v0.4.316_core_requirement.toml"
INCIDENT_INFRA_PIN = FIXTURES / "omnibase_infra_44542e2a0_core_pin.toml"

# omnibase_infra commit -> the omnibase-core version its [tool.uv]
# override-dependencies pinned there, oldest first. 44542e2a0 is the commit the
# incident ran against; 291f2f747 is omnibase_infra dev when this was recorded;
# d20586c7d (omnibase_infra#5110) pins 0.47.48, which admits the 0.47.47 floor.
INFRA_CORE_PINS: tuple[tuple[str, str], ...] = (
    ("44542e2a0", "0.47.44"),
    ("291f2f747", "0.47.46"),
    ("d20586c7d", "0.47.48"),
)

_SPEC = re.compile(r"^\s*omnibase[-_]core\s*(?P<op>==|>=)\s*(?P<version>\d+(?:\.\d+)*)")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def core_bound(requirements: list[str], operator: str) -> tuple[int, ...]:
    """Version of the one omnibase-core entry in ``requirements``, which must use ``operator``."""
    found = [m for m in map(_SPEC.match, requirements) if m is not None]
    assert len(found) == 1, f"expected one omnibase-core entry, got {requirements}"
    assert found[0].group("op") == operator, f"not a {operator} entry: {requirements}"
    return _version_tuple(found[0].group("version"))


def refusal(floor: tuple[int, ...], infra_pin: tuple[int, ...]) -> str | None:
    """Reason a core floor is refused against an infra pin, or None when it fits."""
    if floor > infra_pin:
        return (
            f"omnibase-core>={'.'.join(map(str, floor))} is newer than the "
            f"omnibase_infra pin {'.'.join(map(str, infra_pin))}"
        )
    return None


def _newest_infra_pin() -> tuple[int, ...]:
    return max(_version_tuple(pin) for _, pin in INFRA_CORE_PINS)


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_recorded_incident_pair_is_refused() -> None:
    """Positive control: the v0.4.316 requirement against the 44542e2a0 infra pin."""
    floor = core_bound(_load(INCIDENT_REQUIREMENT)["project"]["dependencies"], ">=")
    infra_pin = core_bound(
        _load(INCIDENT_INFRA_PIN)["tool"]["uv"]["override-dependencies"], "=="
    )

    assert floor == (0, 47, 46)
    assert infra_pin == (0, 47, 44)
    assert (
        refusal(floor, infra_pin)
        == "omnibase-core>=0.47.46 is newer than the omnibase_infra pin 0.47.44"
    )


def test_floor_at_or_below_the_infra_pin_is_accepted() -> None:
    """Negative control: a floor that fits the pin is not refused."""
    assert refusal((0, 47, 46), (0, 47, 46)) is None
    assert refusal((0, 47, 44), (0, 47, 46)) is None


def test_recorded_infra_pins_include_the_incident_pin() -> None:
    """The literal list holds the recorded incident pin and is ordered oldest first."""
    pinned = _load(INCIDENT_INFRA_PIN)["tool"]["uv"]["override-dependencies"]
    assert INFRA_CORE_PINS[0] == (
        "44542e2a0",
        ".".join(map(str, core_bound(pinned, "=="))),
    )
    versions = [_version_tuple(pin) for _, pin in INFRA_CORE_PINS]
    assert versions == sorted(versions)


def test_project_dependency_floor_fits_the_newest_infra_pin() -> None:
    """[project].dependencies does not require a core newer than omnibase_infra pins."""
    floor = core_bound(_load(PYPROJECT)["project"]["dependencies"], ">=")
    assert refusal(floor, _newest_infra_pin()) is None


def test_override_floor_fits_the_newest_infra_pin() -> None:
    """[tool.uv] override-dependencies does not require a core newer than infra pins."""
    floor = core_bound(_load(PYPROJECT)["tool"]["uv"]["override-dependencies"], ">=")
    assert refusal(floor, _newest_infra_pin()) is None
