# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20752: `onex dashboard` serves a bundle whose Overview shows Baseline spend.

The served metering-summary.v1 row has always carried counterfactual_usd, but a
page built before omnidash v1.1.6 has no card for it, so the Overview showed no
Baseline spend however the server answered. Pinning an older bundle would bring
that back with every server-side test still green, so the pin's floor is
asserted here.
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release with the Overview's Baseline spend card
# (OmniNode-ai/omnidash#375).
_FIRST_BASELINE_SPEND_RELEASE = (1, 1, 6)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_shows_baseline_spend() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_BASELINE_SPEND_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
