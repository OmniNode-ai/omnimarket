# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20758: `onex dashboard` serves a bundle whose Overview titles match the mockup.

omnidash v1.1.8 retitled three Overview cards Actual spend, Agent calls and
Tokens processed (they were Spend, Measured runs and Tokens in and out). A page
built before v1.1.8 still shows the old titles, so pinning an older bundle
would fail OMN-20223's AC4 again with every server-side test still green; the
pin's floor is asserted here.
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release with the mockup's titles (OmniNode-ai/omnidash#378).
_FIRST_MOCKUP_TITLES_RELEASE = (1, 1, 8)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_titles_the_overview_as_the_mockup() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_MOCKUP_TITLES_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
