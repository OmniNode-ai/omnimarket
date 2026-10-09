# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20753: `onex dashboard` serves a bundle whose Runs shows a run's status and cost.

The decisions exposure now serves terminal_ok and cost_usd, but a page built
before omnidash v1.1.7 reads a local 1/0 verdict as Not recorded and takes cost
only from a savings session the local store never serves, so Runs showed Status
and Cost Not recorded however the server answered. Pinning an older bundle would
bring that back with every server-side test still green, so the pin's floor is
asserted here.
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release whose Runs reads a run's status and cost from the
# local store (OmniNode-ai/omnidash#376).
_FIRST_STATUS_AND_COST_RELEASE = (1, 1, 7)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_shows_run_status_and_cost() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_STATUS_AND_COST_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
