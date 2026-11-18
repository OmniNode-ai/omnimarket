# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23212: `onex dashboard` serves OmniDash v1.1.13, pinned with its sha256.

Amendment 1 (2026-11-17): the pin moves from v1.1.12 to omnidash v1.1.13
(OMN-24224, tag on omnidash#521's merge 8c242112), which carries #517, #518, #521 and
#523: one missing-value word and one precision per quantity on every local page, a
named failed read on Delegation Evidence ("Run steps is unavailable."), and Settings'
baseline price per million tokens. v1.1.12's bundle carries neither marker.

Failure modes:

* the pin names another release, or the right release with another digest;
* the pinned asset does not verify against the pin through ``ensure_bundle`` (the
  path `onex dashboard` serves from), or is a build without the cursor walk or
  without v1.1.13's pages;
* the release's ``.sha256`` sidecar names another digest or asset than the pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.github_api import download_release_asset
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    ensure_bundle,
    load_pin,
)

_RELEASE = "v1.1.13"
_RELEASE_SHA256 = "9171c1815d8145e6b93a395105d07e4d14b5ef80241bf647ed09b86688978cd6"
_RELEASE_ASSET = "omnidash-bundle-v1.1.13.tar.gz"


@pytest.mark.unit
def test_the_pin_names_v1_1_13_with_its_published_digest() -> None:
    pin = load_pin()
    assert pin.version == _RELEASE
    assert pin.sha256 == _RELEASE_SHA256
    assert pin.asset == _RELEASE_ASSET


@pytest.mark.integration
def test_the_pinned_asset_verifies_and_carries_the_cursor_walk(tmp_path: Path) -> None:
    root = ensure_bundle(load_pin(), cache_root=tmp_path)
    assert (root / "index.html").is_file()
    scripts = sorted(root.rglob("*.js"))
    assert scripts, "the pinned bundle has no scripts"
    assert any(
        "next_cursor" in script.read_text(encoding="utf-8", errors="replace")
        for script in scripts
    ), "the pinned bundle does not follow next_cursor past the newest 500 runs"


@pytest.mark.integration
def test_the_pinned_asset_carries_v1_1_13_pages(tmp_path: Path) -> None:
    root = ensure_bundle(load_pin(), cache_root=tmp_path)
    text = "\n".join(
        script.read_text(encoding="utf-8", errors="replace")
        for script in root.rglob("*.js")
    )
    # omnidash#518: Delegation Evidence names a failed read; #521: Settings shows the price itself.
    for marker in ("Run steps is unavailable", "per million tokens"):
        assert marker in text, f"the pinned bundle lacks v1.1.13's {marker!r}"


@pytest.mark.integration
def test_the_release_sidecar_names_the_pinned_digest_and_asset(tmp_path: Path) -> None:
    pin = load_pin()
    sidecar = tmp_path / f"{_RELEASE_ASSET}.sha256"
    download_release_asset(f"{pin.url}.sha256", sidecar)
    assert sidecar.read_text(encoding="utf-8").split() == [
        _RELEASE_SHA256,
        _RELEASE_ASSET,
    ]
