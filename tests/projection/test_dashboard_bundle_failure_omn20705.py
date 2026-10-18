# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``onex dashboard`` names a missing bundle and announces only pages it holds (OMN-20705).

The quickstart promises that a damaged or unreachable dashboard bundle makes the
command "say so and serve nothing". Before this change the command printed its
start URL first and only then fetched the bundle, so a failed fetch arrived as a
raw traceback after an address had already been handed out. These tests drive
the real command and the real ``ensure_bundle`` with only the network faked:
an unreachable asset and a checksum mismatch each exit non-zero with the reason
on stderr and no address, and a good bundle is verified before the address is
printed, which then answers 200 with the pages.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from omnimarket.cli import cli_local
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_local_dashboard_serve_effect import bundle as bundle_mod
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import BundlePin
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers import (
    handler_local_dashboard_serve as handler_mod,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def dashboard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fixture to set up shared mocks and environment variables for local dashboard CLI tests."""
    cache_root = tmp_path / "cache"
    overlay = tmp_path / "bind.yaml"
    overlay.write_text('dashboard:\n  bind: "127.0.0.1:47231"\n', encoding="utf-8")

    monkeypatch.setattr(bundle_mod, "_CACHE_ROOT", cache_root)
    monkeypatch.setattr(cli_local, "read_local_tenant_identity", lambda: None)
    monkeypatch.setattr(handler_mod, "build_projection_topic_map", lambda: {})
    monkeypatch.setattr(handler_mod, "resolve_local_row_source", lambda: None)

    # Prepare valid bundle assets
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    index_file = pages_dir / "index.html"
    index_file.write_text("<!doctype html><title>OmniDash</title>", encoding="utf-8")

    archive_path = tmp_path / "published.tar.gz"

    def _pack(root: Path, archive: Path) -> str:
        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(root.rglob("*")):
                tar.add(item, arcname=str(item.relative_to(root)))
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    real_sha256 = _pack(pages_dir, archive_path)
    real_archive_bytes = archive_path.read_bytes()

    default_pin = BundlePin(
        version="v1.0.0",
        sha256=real_sha256,
        asset="omnidash-bundle-v1.0.0.tar.gz",
        repository="OmniNode-ai/omnidash",
    )
    monkeypatch.setattr(bundle_mod, "load_pin", lambda: default_pin)

    serve_calls: list[dict[str, Any]] = []

    async def fake_serve(app: Any, host: str, port: int) -> None:
        serve_calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr(handler_mod, "_serve_with_uvicorn", fake_serve)

    return {
        "tmp_path": tmp_path,
        "overlay": overlay,
        "cache_root": cache_root,
        "real_archive_bytes": real_archive_bytes,
        "real_sha256": real_sha256,
        "serve_calls": serve_calls,
    }


def test_an_unreachable_bundle_exits_non_zero_and_names_the_failure_on_stderr(
    dashboard_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: Unreachable bundle download exits non-zero, names failure on stderr, no address printed."""

    def fake_download(url: str, to: Path) -> None:
        raise GitHubApiError("<urlopen error [Errno 61] Connection refused>")

    monkeypatch.setattr(bundle_mod, "download_release_asset", fake_download)

    runner = CliRunner()
    result = runner.invoke(
        cli_local.dashboard_command, ["--overlay", str(dashboard_env["overlay"])]
    )

    assert result.exit_code == 1
    assert "could not download the dashboard bundle" in result.stderr
    assert "nothing was served" in result.stderr
    assert "onex dashboard: http" not in result.stdout
    assert "Traceback" not in result.output
    assert len(dashboard_env["serve_calls"]) == 0


def test_a_bundle_failing_its_checksum_exits_non_zero_and_names_the_failure_on_stderr(
    dashboard_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: Bundle failing checksum exits non-zero, names failure on stderr, discards archive."""

    def fake_download(url: str, to: Path) -> None:
        to.parent.mkdir(parents=True, exist_ok=True)
        to.write_bytes(dashboard_env["real_archive_bytes"])

    monkeypatch.setattr(bundle_mod, "download_release_asset", fake_download)

    # Force a mismatch by changing the pin's expected sha256
    bad_pin = BundlePin(
        version="v1.0.0",
        sha256="0" * 64,
        asset="omnidash-bundle-v1.0.0.tar.gz",
        repository="OmniNode-ai/omnidash",
    )
    monkeypatch.setattr(bundle_mod, "load_pin", lambda: bad_pin)

    runner = CliRunner()
    result = runner.invoke(
        cli_local.dashboard_command, ["--overlay", str(dashboard_env["overlay"])]
    )

    assert result.exit_code == 1
    assert "does not match the pin" in result.stderr
    assert "onex dashboard: http" not in result.stdout
    assert len(dashboard_env["serve_calls"]) == 0

    # Ensure archive file under cache was discarded (no *.tar.gz under cache_root)
    tar_files = list(dashboard_env["cache_root"].glob("**/*.tar.gz"))
    assert len(tar_files) == 0


def test_the_printed_root_serves_the_verified_pages(
    dashboard_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: Verified bundle downloads, prints start URL once verified, serves index.html on HTTP 200."""
    events: list[str] = []
    echo_records: list[tuple[str, str]] = []

    def fake_download(url: str, to: Path) -> None:
        events.append("download")
        to.parent.mkdir(parents=True, exist_ok=True)
        to.write_bytes(dashboard_env["real_archive_bytes"])

    monkeypatch.setattr(bundle_mod, "download_release_asset", fake_download)

    original_echo = click.echo

    def recorder(message: Any = None, *args: Any, **kwargs: Any) -> Any:
        echo_records.append(("echo", str(message)))
        events.append("echo")
        return original_echo(message, *args, **kwargs)

    monkeypatch.setattr(click, "echo", recorder)

    runner = CliRunner()
    result = runner.invoke(
        cli_local.dashboard_command, ["--overlay", str(dashboard_env["overlay"])]
    )

    assert result.exit_code == 0
    assert "download" in events
    assert "echo" in events
    assert events.index("download") < events.index("echo")

    # Parse printed URL
    start_line = next(msg for _, msg in echo_records if "onex dashboard:" in msg)
    match = re.search(r"http://127\.0\.0\.1:47231/\?token=(\S+)", start_line)
    assert match is not None
    token = match.group(1)

    assert len(dashboard_env["serve_calls"]) == 1
    app = dashboard_env["serve_calls"][0]["app"]

    client = TestClient(app)
    response = client.get(f"/?token={token}", headers={"Host": "127.0.0.1:47231"})
    assert response.status_code == 200
    assert "OmniDash" in response.text
