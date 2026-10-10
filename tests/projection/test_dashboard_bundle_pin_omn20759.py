# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20759: `onex dashboard` serves a bundle whose run tables fit their panels at 1440x900.

Before omnidash v1.1.9 the Runs table scrolled sideways with Host clipped and the
Overview's Recent runs clipped the Run id at 1440, however the server answered.
Pinning an older bundle would bring that back with every server-side test still
green, so the pin's floor is asserted here. v1.1.9 also carries the mockup's
card titles (OMN-20758, omnidash#378).
"""

from __future__ import annotations

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import load_pin

# The first omnidash release whose Runs and Recent runs tables fit at 1440x900
# (OmniNode-ai/omnidash#379).
_FIRST_FITTED_TABLES_RELEASE = (1, 1, 9)


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_fits_the_run_tables() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_FITTED_TABLES_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"
