# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard`` serves its pinned pages on a first run that has no network.

The C30 Claude Code plugin delegate check found its only failing clause was
"onex dashboard serves on loopback": the command downloaded the pages bundle
from GitHub before it printed a URL, the probe's scratch HOME made every run a
cold download, and a stalled download outlasted the probe's 30 s wait (32.0 s
stalled against 5.3 s normal, reproduced on a lab host). A customer on a slow
or offline network meets the same wait.

The pinned bundle therefore ships inside the installed package, split into
parts that each stay under the repository's large-file limit, and is
reassembled and verified against the pin in this repository's own
``pyproject.toml``. The network fetch stays only as the update path, for a pin
that has moved past what the package carries.

These cases fail on the download-first behaviour: each one makes the download
raise, so a start that reaches for the network is a failed start.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect import bundle as bundle_mod
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    BundlePin,
    DashboardBundleError,
    ensure_bundle,
    load_pin,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_local_dashboard_serve_effect"
    / "packaged_bundle"
)
# The repository's check-added-large-files limit is 1000 KB; a part stays under it.
_PART_LIMIT_BYTES = 1000 * 1024


class _ReadNode:
    async def handle(
        self, request: ModelProjectionReadRequest
    ) -> ModelProjectionReadResult:
        return ModelProjectionReadResult(
            topic=request.topic,
            ok=True,
            http_status=200,
            response={"rows": [], "row_count": 0, "latest_event_at": None},
        )


class _Network:
    """A stand-in for the bundle download that records every reach for the network."""

    def __init__(self, archive: Path | None = None) -> None:
        self.calls: list[str] = []
        self._archive = archive

    def __call__(self, url: str, to: Path) -> None:
        self.calls.append(url)
        if self._archive is None:
            raise DashboardBundleError(f"the network was used: {url}")
        to.parent.mkdir(parents=True, exist_ok=True)
        to.write_bytes(self._archive.read_bytes())


def _pack(root: Path, archive: Path) -> str:
    with tarfile.open(archive, "w:gz") as tar:
        for item in sorted(root.rglob("*")):
            tar.add(item, arcname=str(item.relative_to(root)))
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def _synthetic(tmp_path: Path, asset: str = "dash.tar.gz") -> tuple[Path, BundlePin]:
    pages = tmp_path / "src-pages"
    pages.mkdir()
    (pages / "index.html").write_text("<!doctype html><title>dash</title>", "utf-8")
    archive = tmp_path / asset
    digest = _pack(pages, archive)
    pin = BundlePin(
        version="v1.0.0",
        sha256=digest,
        asset=asset,
        repository="OmniNode-ai/omnidash",
    )
    return archive, pin


def _split(archive: Path, into: Path, size: int) -> list[Path]:
    into.mkdir(parents=True, exist_ok=True)
    data = archive.read_bytes()
    parts: list[Path] = []
    for n, start in enumerate(range(0, len(data), size)):
        part = into / f"{archive.name}.part-{n:02d}"
        part.write_bytes(data[start : start + size])
        parts.append(part)
    return parts


# -- the package carries the pinned bundle -------------------------------


def test_the_packaged_parts_are_exactly_the_pinned_bundle() -> None:
    """Moving the pin without moving the packaged bytes fails here, by name.

    To move both: download the new asset, compute its sha256 and paste it into
    ``[tool.onex.dashboard_bundle]``, then replace the parts with
    ``split -b 720000 -d <asset> <asset>.part-`` run in
    ``src/omnimarket/nodes/node_local_dashboard_serve_effect/packaged_bundle``
    (delete the old parts first; GNU split names them ``part-00``, ``part-01``).
    """
    pin = load_pin(_REPO_ROOT / "pyproject.toml")
    parts = sorted(_PACKAGED.glob(f"{pin.asset}.part-*"))
    assert parts, (
        f"no packaged parts for the pinned asset {pin.asset} in {_PACKAGED}; the "
        "installed package would fall back to a network download on first run"
    )
    stale = sorted(
        p.name for p in _PACKAGED.iterdir() if not p.name.startswith(f"{pin.asset}.")
    )
    assert not stale, f"packaged parts of an older pin are still shipped: {stale}"
    assembled = b"".join(p.read_bytes() for p in parts)
    assert hashlib.sha256(assembled).hexdigest() == pin.sha256


def test_every_packaged_part_stays_under_the_large_file_limit() -> None:
    parts = sorted(_PACKAGED.glob("*.part-*"))
    assert parts
    assert all(p.stat().st_size < _PART_LIMIT_BYTES for p in parts)


# -- a cold first run with no network serves the pages -------------------


def test_a_cold_first_run_with_no_network_serves_the_pinned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden chain: the real pin, an empty cache, a download that must not happen."""
    network = _Network()
    monkeypatch.setattr(bundle_mod, "_download", network)
    pin = load_pin()
    cache = tmp_path / "scratch-home" / "bundles"

    root = ensure_bundle(pin, cache_root=cache)

    assert network.calls == []
    assert (root / "index.html").is_file()
    assert root == cache / pin.sha256
    # The verified archive is left beside the extracted pages, so the next start
    # re-verifies the bytes it serves exactly as a downloaded copy is.
    archive = root.with_suffix(".tar.gz")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == pin.sha256

    client = TestClient(
        create_dashboard_app(handler=_ReadNode(), tenant="t", topic_map={}, pages=root)
    )
    page = client.get("/")
    assert page.status_code == 200
    assert "<html" in page.text.lower() or "<!doctype" in page.text.lower()

    # A second start finds the verified cache and still reaches for nothing.
    assert ensure_bundle(pin, cache_root=cache) == root
    assert network.calls == []


def test_a_first_run_with_downloading_disabled_still_serves_the_packaged_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_mod, "_download", _Network())
    root = ensure_bundle(
        load_pin(), cache_root=tmp_path / "bundles", allow_download=False
    )
    assert (root / "index.html").is_file()


# -- error chain: a packaged copy that is not the pin is refused ---------


def test_a_tampered_packaged_part_is_refused_with_no_download_and_nothing_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, pin = _synthetic(tmp_path)
    packaged = tmp_path / "packaged"
    parts = _split(archive, packaged, size=64)
    assert len(parts) > 1
    flipped = bytearray(parts[-1].read_bytes())
    flipped[0] ^= 0xFF
    parts[-1].write_bytes(bytes(flipped))

    network = _Network(archive)
    monkeypatch.setattr(bundle_mod, "_download", network)
    cache = tmp_path / "cache"
    with pytest.raises(DashboardBundleError, match="packaged dashboard bundle"):
        ensure_bundle(pin, cache_root=cache, packaged_root=packaged)

    # Refused, not repaired: a download here would turn tampering into a cache miss.
    assert network.calls == []
    assert not (cache / pin.sha256).exists()
    assert not (cache / f"{pin.sha256}.tar.gz").exists()


def test_a_truncated_packaged_bundle_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, pin = _synthetic(tmp_path)
    packaged = tmp_path / "packaged"
    parts = _split(archive, packaged, size=64)
    parts[-1].unlink()

    network = _Network(archive)
    monkeypatch.setattr(bundle_mod, "_download", network)
    with pytest.raises(DashboardBundleError, match="does not match the pin"):
        ensure_bundle(pin, cache_root=tmp_path / "cache", packaged_root=packaged)
    assert network.calls == []


# -- the network is the update path, for a pin the package does not carry


def test_a_pin_the_package_does_not_carry_is_fetched_once_as_the_update_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, pin = _synthetic(tmp_path, asset="newer.tar.gz")
    network = _Network(archive)
    monkeypatch.setattr(bundle_mod, "_download", network)
    empty = tmp_path / "packaged"
    empty.mkdir()
    cache = tmp_path / "cache"

    root = ensure_bundle(pin, cache_root=cache, packaged_root=empty)
    assert (root / "index.html").is_file()
    assert ensure_bundle(pin, cache_root=cache, packaged_root=empty) == root
    assert len(network.calls) == 1


def test_a_packaged_copy_that_matches_the_pin_is_installed_without_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, pin = _synthetic(tmp_path)
    packaged = tmp_path / "packaged"
    _split(archive, packaged, size=64)
    network = _Network()
    monkeypatch.setattr(bundle_mod, "_download", network)

    root = ensure_bundle(pin, cache_root=tmp_path / "cache", packaged_root=packaged)
    assert (root / "index.html").read_text("utf-8").startswith("<!doctype html>")
    assert network.calls == []
