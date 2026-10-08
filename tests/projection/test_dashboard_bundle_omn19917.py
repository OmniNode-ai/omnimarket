# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard`` serves pinned pages, or serves nothing (OMN-19917, plan T2.9).

Decision D8 (a): a developer with no Node and no clone runs ``onex dashboard``
and gets the dashboard, so the command downloads OmniDash's prebuilt pages and
verifies them against the digest this repository pins. The verification is the
whole point of the design, so these tests are written from the two ways it can
be wrong rather than from the happy path: a bundle that does not match the pin
must be refused and NOT served, and a page must never be able to shadow the API
the dashboard reads.

The one thing a test cannot check is the digest itself matching a bundle nobody
has built yet, so the pin's value is checked for shape and agreement with what
ships in the wheel; that the pinned digest is the published asset's is proven by
the download in a clean container, which is the acceptance criterion.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
import tomllib
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


class _ReadNode:
    """Enough of the read node to prove the API routes still answer."""

    async def handle(
        self, request: ModelProjectionReadRequest
    ) -> ModelProjectionReadResult:
        return ModelProjectionReadResult(
            topic=request.topic,
            ok=True,
            http_status=200,
            response={"rows": [], "row_count": 0, "latest_event_at": None},
        )


def _pages(tmp_path: Path) -> Path:
    root = tmp_path / "pages"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>dash</title>", "utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", "utf-8")
    return root


def _app(tmp_path: Path) -> TestClient:
    return TestClient(
        create_dashboard_app(
            handler=_ReadNode(), tenant="t", topic_map={}, pages=_pages(tmp_path)
        )
    )


def _pack(root: Path, archive: Path) -> str:
    with tarfile.open(archive, "w:gz") as tar:
        for item in sorted(root.rglob("*")):
            tar.add(item, arcname=str(item.relative_to(root)))
    return hashlib.sha256(archive.read_bytes()).hexdigest()


# -- the pin -------------------------------------------------------------


def test_the_repository_pins_a_bundle() -> None:
    pin = load_pin(_REPO_ROOT / "pyproject.toml")
    assert pin.repository == "OmniNode-ai/omnidash"
    assert len(pin.sha256) == 64
    assert pin.url.startswith("https://github.com/OmniNode-ai/omnidash/releases/")
    assert pin.asset in pin.url
    assert pin.version in pin.url


def test_the_pin_ships_inside_the_wheel() -> None:
    """The pin must be readable from an installed copy, which has no pyproject.toml.

    This is the defect the force-include exists to prevent: a pin read from the
    source tree verifies nothing on the only install D8 describes.
    """
    build = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = build["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included["pyproject.toml"] == "omnimarket/dashboard_bundle_pin.toml"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "[tool.onex.dashboard_bundle]\nversion = 'v1'\n",
        "[tool.onex.dashboard_bundle]\nversion = 'v1'\nsha256 = 'nothex'\n"
        "asset = 'a.tar.gz'\nrepository = 'o/r'\n",
    ],
    ids=["no-table", "incomplete", "not-a-digest"],
)
def test_an_unusable_pin_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(DashboardBundleError):
        load_pin(path)


# -- a bundle that does not match the pin is refused ---------------------


def test_a_tampered_download_is_discarded_and_nothing_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _pages(tmp_path)
    archive = tmp_path / "evil.tar.gz"
    _pack(served, archive)
    pin = BundlePin(
        version="v9.9.9",
        sha256="0" * 64,
        asset="evil.tar.gz",
        repository="OmniNode-ai/omnidash",
    )

    def _fake_download(url: str, to: Path) -> None:
        to.parent.mkdir(parents=True, exist_ok=True)
        to.write_bytes(archive.read_bytes())

    monkeypatch.setattr(bundle_mod, "_download", _fake_download)
    cache = tmp_path / "cache"
    with pytest.raises(DashboardBundleError, match="does not match the pin"):
        ensure_bundle(pin, cache_root=cache)
    # Refused, not quietly repaired: nothing was left for a later start to serve.
    assert not (cache / pin.sha256).exists()


def test_a_cache_that_changed_after_it_was_written_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _pages(tmp_path)
    archive = tmp_path / "good.tar.gz"
    digest = _pack(served, archive)
    pin = BundlePin(
        version="v1.0.0",
        sha256=digest,
        asset="good.tar.gz",
        repository="OmniNode-ai/omnidash",
    )

    calls: list[str] = []

    def _fake_download(url: str, to: Path) -> None:
        calls.append(url)
        to.parent.mkdir(parents=True, exist_ok=True)
        to.write_bytes(archive.read_bytes())

    monkeypatch.setattr(bundle_mod, "_download", _fake_download)
    cache = tmp_path / "cache"
    root = ensure_bundle(pin, cache_root=cache)
    assert (root / "index.html").is_file()
    # Cached: a second start fetches nothing.
    assert ensure_bundle(pin, cache_root=cache) == root
    assert len(calls) == 1

    (cache / pin.sha256).with_suffix(".tar.gz").write_bytes(b"swapped")
    with pytest.raises(DashboardBundleError, match="cached dashboard bundle"):
        ensure_bundle(pin, cache_root=cache)
    # It did not re-download, which would have turned an attack into a cache miss.
    assert len(calls) == 1


def test_a_member_that_escapes_the_extraction_directory_is_refused(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "escape.tar.gz"
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "owned").write_text("x", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload / "owned", arcname="../../owned")
    with pytest.raises(DashboardBundleError, match="escapes"):
        bundle_mod._extract(archive, tmp_path / "into")


def test_a_bundle_with_no_index_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    (empty / "assets").mkdir(parents=True)
    (empty / "assets" / "app.js").write_text("1", encoding="utf-8")
    archive = tmp_path / "noindex.tar.gz"
    digest = _pack(empty, archive)
    pin = BundlePin(
        version="v1.0.0",
        sha256=digest,
        asset="noindex.tar.gz",
        repository="OmniNode-ai/omnidash",
    )
    monkeypatch.setattr(
        bundle_mod,
        "_download",
        lambda _url, to: (
            to.parent.mkdir(parents=True, exist_ok=True),
            to.write_bytes(archive.read_bytes()),
        )[0],
    )
    with pytest.raises(DashboardBundleError, match=re.escape("no index.html")):
        ensure_bundle(pin, cache_root=tmp_path / "cache")


def test_an_absent_bundle_is_named_rather_than_served_empty(tmp_path: Path) -> None:
    pin = BundlePin(
        version="v1.0.0",
        sha256="a" * 64,
        asset="dash.tar.gz",
        repository="OmniNode-ai/omnidash",
    )
    with pytest.raises(DashboardBundleError, match="not cached"):
        ensure_bundle(pin, cache_root=tmp_path / "cache", allow_download=False)


# -- what the port serves once the bundle is verified -------------------


def test_the_root_path_serves_the_dashboard(tmp_path: Path) -> None:
    """The defect T2.9 exists to fix: ``/`` was 404 because no page was mounted."""
    response = _app(tmp_path).get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "dash" in response.text


def test_an_asset_is_served_from_the_bundle(tmp_path: Path) -> None:
    response = _app(tmp_path).get("/assets/app.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)"


def test_a_client_route_falls_back_to_the_index(tmp_path: Path) -> None:
    response = _app(tmp_path).get("/delegations")
    assert response.status_code == 200
    assert "dash" in response.text


def test_a_missing_asset_is_a_404_not_the_index(tmp_path: Path) -> None:
    """Served as HTML it would fail in the browser with an unreadable MIME error."""
    response = _app(tmp_path).get("/assets/gone.js")
    assert response.status_code == 404
    assert response.json()["error"] == "asset_not_found"


def test_a_page_cannot_shadow_the_api(tmp_path: Path) -> None:
    """Registration order is load-bearing: the catch-all is registered last."""
    client = _app(tmp_path)
    assert client.get("/projections").json() == {"topics": []}
    assert client.get("/projection/onex.decisions").json()["rows"] == []


def test_a_crafted_path_cannot_reach_outside_the_bundle(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve me", encoding="utf-8")
    client = _app(tmp_path)
    for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
        response = client.get(path)
        assert "do not serve me" not in response.text, path


def test_no_pages_means_the_api_alone(tmp_path: Path) -> None:
    """The data-only app stays constructible, so the mount is additive."""
    client = TestClient(
        create_dashboard_app(handler=_ReadNode(), tenant="t", topic_map={})
    )
    assert client.get("/projections").status_code == 200
    assert client.get("/").status_code == 404
