# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node tests that render an installer unit with default overlay roots need no lab host table (OMN-19998).

The registry unit installer fills ``{overlays_root}`` from the lab host table row of the host the request names. A test
that does not exercise that resolution (it renders some other part of a unit) runs with the layout every Linux lab host
declares, the overlays clone under the registry root. A test that injects its own ``hosts`` loader gets the real
resolution, and ``test_overlay_root_declared_by_host_table`` covers it.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers import (
    handler_registry_units as installer,
)


@pytest.fixture(autouse=True)
def installer_default_overlays_root(monkeypatch: pytest.MonkeyPatch) -> None:
    real = installer.HandlerRegistryUnits.overlays_root

    def overlays_root(self: installer.HandlerRegistryUnits, request: object) -> str:
        if self.hosts is installer.default_hosts:
            return f"{request.registry_root}/overlays/overlays"  # type: ignore[attr-defined]
        return real(self, request)  # type: ignore[arg-type]

    monkeypatch.setattr(installer.HandlerRegistryUnits, "overlays_root", overlays_root)
