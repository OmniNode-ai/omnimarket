# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_issuer_overlay_delivery_effect: overlay delivered to the issuer host, read back (OMN-21899)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_issuer_overlay_delivery_effect as node_pkg
from omnimarket.nodes.node_issuer_overlay_delivery_effect.handlers.handler_overlay_delivery import (
    HandlerOverlayDelivery,
)
from omnimarket.nodes.node_issuer_overlay_delivery_effect.models.model_overlay_delivery import (
    ModelOverlayDeliveryRequest,
)

pytestmark = pytest.mark.unit

OVERLAY = "schema_version: 1\nprincipals: []\n"


class FakeHost:
    """In-memory stand-in for the issuer host's files."""

    def __init__(
        self, files: dict[str, str] | None = None, drop_writes: bool = False
    ) -> None:
        self.files = dict(files or {})
        self.drop_writes = drop_writes

    def write_file(self, path: str, content: str) -> None:
        if not self.drop_writes:
            self.files[path] = content

    def read_file(self, path: str) -> str | None:
        return self.files.get(path)


def _request(**kw: str) -> ModelOverlayDeliveryRequest:
    base = {
        "overlay_content": OVERLAY,
        "overlay_remote_path": "/srv/issuer/overlay.yaml",
        "env_file_path": "/srv/issuer/issuer.env",
    }
    base.update(kw)
    return ModelOverlayDeliveryRequest(**base)


def test_overlay_delivery_writes_overlay_and_env_and_reads_back() -> None:
    host = FakeHost({"/srv/issuer/issuer.env": "OTHER=1\n"})
    result = HandlerOverlayDelivery(host).handle(_request())
    assert result.delivered is True
    assert host.files["/srv/issuer/overlay.yaml"] == OVERLAY
    assert (
        "PRINCIPAL_ISSUER_OVERLAY_FILE=/srv/issuer/overlay.yaml"
        in host.files["/srv/issuer/issuer.env"].splitlines()
    )
    assert "OTHER=1" in host.files["/srv/issuer/issuer.env"].splitlines()
    assert result.overlay_sha256 == hashlib.sha256(OVERLAY.encode()).hexdigest()
    assert result.env_readback_value == "/srv/issuer/overlay.yaml"


def test_overlay_delivery_replaces_a_stale_env_line_once() -> None:
    host = FakeHost(
        {"/srv/issuer/issuer.env": "PRINCIPAL_ISSUER_OVERLAY_FILE=/old\nA=b\n"}
    )
    HandlerOverlayDelivery(host).handle(_request())
    lines = host.files["/srv/issuer/issuer.env"].splitlines()
    assert lines.count("PRINCIPAL_ISSUER_OVERLAY_FILE=/srv/issuer/overlay.yaml") == 1
    assert not any(line.endswith("/old") for line in lines)


def test_overlay_delivery_not_delivered_when_host_readback_differs() -> None:
    host = FakeHost(drop_writes=True)
    result = HandlerOverlayDelivery(host).handle(_request())
    assert result.delivered is False
    assert "readback" in result.reason


def test_overlay_delivery_contract_carries_no_host_path_literal() -> None:
    text = (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    assert yaml.safe_load(text)["name"] == "node_issuer_overlay_delivery_effect"
    assert "/srv" not in text
    assert "100." not in text
