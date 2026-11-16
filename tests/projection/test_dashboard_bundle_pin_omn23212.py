# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23212: `onex dashboard` serves OmniDash v1.1.12, pinned with its sha256.

omnidash v1.1.12 (OMN-23211, tag on omnidash#506's merge 34e082d9) carries the 32
changes since v1.1.11, among them the decisions walk past the newest 500 runs
(its HTTP source follows ``next_cursor``). The pin moves to it here.

Failure modes:

* the pin names another release, or the right release with another digest;
* the pinned asset does not verify against the pin through ``ensure_bundle`` (the
  path `onex dashboard` serves from), or is a build without the cursor walk;
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

_RELEASE = "v1.1.12"
_RELEASE_SHA256 = "ec9b3f16c53b7cf77d53111bef587dbec40cdc837afb8cdc6e2be53353868de7"
_RELEASE_ASSET = "omnidash-bundle-v1.1.12.tar.gz"


@pytest.mark.unit
def test_the_pin_names_v1_1_12_with_its_published_digest() -> None:
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
def test_the_release_sidecar_names_the_pinned_digest_and_asset(tmp_path: Path) -> None:
    pin = load_pin()
    sidecar = tmp_path / f"{_RELEASE_ASSET}.sha256"
    download_release_asset(f"{pin.url}.sha256", sidecar)
    assert sidecar.read_text(encoding="utf-8").split() == [
        _RELEASE_SHA256,
        _RELEASE_ASSET,
    ]
