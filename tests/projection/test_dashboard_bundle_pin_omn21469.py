# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21469: `onex dashboard` serves the seven-page OmniDash with one Settings page.

omnidash v1.1.11 is the first release carrying the seven-page redesign
(OmniNode-ai/omnidash#465 to #470): the primary navigation reads Overview, Runs,
Workflow, Usage and Settings, with Model providers, Access and System status
under Settings. v1.1.10 predates all of it, so pinning it serves the old pages
while every server-side test stays green.

Two checks, matching the ticket's criteria:

* the pin's floor (AC3), asserted from the pin alone like the earlier pin tests;
* the pinned asset itself (AC2, AC3): fetched and verified by the same
  ``ensure_bundle`` path `onex dashboard` runs, so a digest that does not match
  the published v1.1.11 asset is refused by the product code, and the pages it
  unpacks must carry the Settings navigation entry. At the v1.1.10 pin the
  download verifies but the entry is absent, so this fails there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    ensure_bundle,
    load_pin,
)

pytestmark = pytest.mark.unit

# The first omnidash release with the seven-page navigation and one Settings page.
_FIRST_SETTINGS_PAGE_RELEASE = (1, 1, 11)

# The navigation label as the built JavaScript spells it; absent from v1.1.10.
_SETTINGS_NAV = re.compile(r'"Settings"|>Settings<|label:"Settings"')


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def test_the_pinned_bundle_is_the_seven_page_release() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_SETTINGS_PAGE_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"


def test_the_pinned_asset_verifies_and_carries_the_settings_page(
    tmp_path: Path,
) -> None:
    root = ensure_bundle(load_pin(), cache_root=tmp_path)
    assert (root / "index.html").is_file()
    scripts = sorted(root.rglob("*.js"))
    assert scripts, "the verified bundle carries no JavaScript"
    assert any(
        _SETTINGS_NAV.search(script.read_text(encoding="utf-8", errors="replace"))
        for script in scripts
    ), "the pinned bundle has no Settings navigation entry"
