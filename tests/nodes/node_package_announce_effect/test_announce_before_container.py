# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Announcing completes before any container exists (OMN-21597, A1 AC3, AC10)."""

import sys
from pathlib import Path
from uuid import uuid5

import pytest
from omnibase_core.models.container.model_onex_container import ModelONEXContainer

from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    ANNOUNCER,
    BUS_BINDINGS,
    AnnounceTestBindings,
    make_overlay,
)


@pytest.mark.unit
async def test_announcer_and_catalog_complete_with_a_container_constructor_that_raises(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[str] = []

    def raising_init(self: ModelONEXContainer, *args: object, **kwargs: object) -> None:
        constructed.append("called")
        raise AssertionError("the container constructor was called during step 0")

    monkeypatch.setattr(ModelONEXContainer, "__init__", raising_init)

    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()

    assert constructed == [], "falsifier: the constructor is called"
    assert len(catalog.snapshot()) == 3


@pytest.mark.unit
async def test_step_zero_imports_no_handler_or_node_module(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
    site: Path,
) -> None:
    """The fixture packages are directories with contracts and no importable code;
    their module roots must not appear in sys.modules after the step."""
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    await step0.run()
    assert "alpha_pkg" not in sys.modules
    assert "beta_pkg" not in sys.modules
    assert str(site) not in sys.path


@pytest.mark.unit
async def test_the_same_scenario_gives_the_same_catalog_on_every_bus(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
) -> None:
    """One code path (INV-047): only the overlay's binding name differs between runs."""
    outcomes = []
    for name in BUS_BINDINGS:
        step0 = AnnounceStep0(
            overlay=make_overlay(name, announcer_id=uuid5(ANNOUNCER, name)),
            bindings=bindings.registry,
        )
        catalog = await step0.run()
        outcomes.append(
            {
                key: (entry.state, entry.content_hash, entry.generation)
                for key, entry in catalog.snapshot().items()
            }
        )
    assert outcomes[0] == outcomes[1]
