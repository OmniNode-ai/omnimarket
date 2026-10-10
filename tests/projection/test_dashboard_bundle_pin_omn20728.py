# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20728: `onex dashboard` serves a bundle that reads as the declared tenant.

The local server names its tenant in ``GET /projections`` (#3557), but a page
built before omnidash v1.1.5 never reads that field and refuses every scoped
panel with Tenant not configured. Pinning an older bundle would therefore
reintroduce the defect with every server-side test still green, so the pin's
floor is asserted here.
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release whose resolver falls back to the catalogue tenant
# (OmniNode-ai/omnidash#374).
_FIRST_CATALOGUE_TENANT_RELEASE = (1, 1, 5)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_reads_the_catalogue_tenant() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_CATALOGUE_TENANT_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
