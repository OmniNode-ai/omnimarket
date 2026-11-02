# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Fixtures of the node announcement tests (OMN-21597)."""

from pathlib import Path

import pytest

from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    BUS_BINDINGS,
    AnnounceTestBindings,
)
from tests.nodes.node_package_announce_effect.helper_announce_site import (
    install_standard_site,
)


@pytest.fixture
def site(tmp_path: Path) -> Path:
    directory = tmp_path / "site"
    directory.mkdir()
    return directory


@pytest.fixture
def standard_contracts(site: Path) -> dict[str, bytes]:
    return install_standard_site(site)


@pytest.fixture
def bindings(site: Path, tmp_path: Path) -> AnnounceTestBindings:
    return AnnounceTestBindings(site, tmp_path / "inventories")


@pytest.fixture(params=BUS_BINDINGS)
def bus_binding(request: pytest.FixtureRequest) -> str:
    """The overlay's bus binding name; the only thing that differs between runs."""
    return str(request.param)
