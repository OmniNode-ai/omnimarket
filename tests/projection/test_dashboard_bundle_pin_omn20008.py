# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20008 AC5: `onex dashboard` serves a bundle that shows the Savings percentage line.

omnidash v1.1.10 is the first release carrying OmniNode-ai/omnidash#384, the
Savings card's "N% below the <baseline> baseline (modelled)" line read from
metering-summary.v1's served savings_pct_of_counterfactual. A pin below it
serves a page with no such line while every server-side test stays green, so
the pin's floor is asserted here.
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release with the Savings percentage line (OmniNode-ai/omnidash#384).
_FIRST_SAVINGS_PERCENTAGE_RELEASE = (1, 1, 10)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_shows_the_savings_percentage_line() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_SAVINGS_PERCENTAGE_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
