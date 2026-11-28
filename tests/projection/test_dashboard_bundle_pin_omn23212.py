# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23212: `onex dashboard` serves OmniDash v1.1.17, pinned with its sha256.

Amendment 1 (2026-11-17): the pin moved from v1.1.12 to omnidash v1.1.13 (OMN-24224),
which carries a named failed read on Delegation Evidence ("Run steps is unavailable.")
and Settings' baseline price per million tokens.

Amendment 2 (2026-11-18): the pin moves to omnidash v1.1.14 (tag on omnidash#527's merge
ac14f48d, which carries the version bump #529 and #528), so an install writes a measured
zero cost with its cents ("$0.00") like every other amount. v1.1.13's bundle drops them
with the short-zero call (_SHORT_ZERO, 6 times); v1.1.14's has none.

Amendment 3 (2026-11-22, OMN-26457): the pin moves to omnidash v1.1.15 (signed tag on omnidash#538's
merge bca36b98), which carries OMN-26439's fix (5870118c): a window with no runs reads
"0 tokens" on the Overview. v1.1.15 still writes a zero amount with its cents.

Amendment 4 (2026-11-26, OMN-26691): the pin moves to omnidash v1.1.16 (signed tag on omnidash#544's
merge fcbd7f90), which carries the Overview Done figure (omnidash#531, a74c18dc) and the Running
figure (omnidash#532, bb23d514). v1.1.16 still writes a zero amount with its cents.

Amendment 5 (2026-11-28, OMN-26767): the pin moves to omnidash v1.1.17 (signed tag on omnidash#546's
merge 46cec44f), which carries the dashboard window, rollout and failed-read fixes (OMN-26665,
omnidash#543) and the safe markdown run page (OMN-26646, omnidash#542).

Failure modes:

* the pin names another release, or the right release with another digest;
* the pinned asset does not verify against the pin through ``ensure_bundle`` (the
  path `onex dashboard` serves from), or is a build without the cursor walk or
  without v1.1.13's pages;
* the pinned asset is a build that still drops the cents from a zero amount;
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

_RELEASE = "v1.1.17"
_RELEASE_SHA256 = "51cb5486a086a6aa35e968055c171adf80081007018312816f7fc14f8bf76def"
_RELEASE_ASSET = "omnidash-bundle-v1.1.17.tar.gz"


@pytest.mark.unit
def test_the_pin_names_v1_1_17_with_its_published_digest() -> None:
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


# The short-zero call v1.1.13 used to write a zero amount without cents (omnidash#528 removed it).
_SHORT_ZERO = '.replace(/\\.0+$/,"")'


@pytest.mark.integration
def test_the_pinned_asset_writes_a_zero_amount_with_its_cents(tmp_path: Path) -> None:
    root = ensure_bundle(load_pin(), cache_root=tmp_path)
    text = "\n".join(
        script.read_text(encoding="utf-8", errors="replace")
        for script in root.rglob("*.js")
    )
    assert _SHORT_ZERO not in text, (
        "the pinned bundle still drops the cents from a zero amount"
    )


@pytest.mark.integration
def test_the_release_sidecar_names_the_pinned_digest_and_asset(tmp_path: Path) -> None:
    pin = load_pin()
    sidecar = tmp_path / f"{_RELEASE_ASSET}.sha256"
    download_release_asset(f"{pin.url}.sha256", sidecar)
    assert sidecar.read_text(encoding="utf-8").split() == [
        _RELEASE_SHA256,
        _RELEASE_ASSET,
    ]
