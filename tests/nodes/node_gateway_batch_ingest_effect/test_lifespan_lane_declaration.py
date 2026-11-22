# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The door's lifespan finds the lane declaration the runtime mounts, from the door's own overlay.

The dev runtime mounts ``config/lab`` read-only at one config root and exports no
``ONEX_WORKSPACE_CONFIG_ROOT`` to the process, so the lane binding the door's producer opens found no
``config/lab/ci_bus_lanes.yaml`` and the lifespan failed. The door's overlay names the mounted root.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_gateway_batch_ingest_effect import asgi
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    OVERLAY_FILE_ENV,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import overlay_document

_CONFIG_ROOT_ENV = "ONEX_WORKSPACE_CONFIG_ROOT"
_NODE = "node_gateway_batch_ingest_effect"


def _declaration(root: Path) -> None:
    path = root / "config" / "lab" / "ci_bus_lanes.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "default": "inmemory",
                "lanes": {
                    "dev": {"broker": "127.0.0.1:1", "security_protocol": "PLAINTEXT"}
                },
            }
        ),
        encoding="utf-8",
    )


def _store(tmp_path: Path, **kafka: object) -> Path:
    """An overlay store root holding the door's overlay, the way the runtime's overlay roots hold it."""
    headroom = asgi.mount_ceiling()[1] + 1
    document = overlay_document(
        chunk_headroom_bytes=headroom, max_inflight_bytes=4 * (16384 + headroom)
    )
    document["kafka"] = {**document["kafka"], **kafka}
    path = tmp_path / "store" / _NODE / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path.parent.parent


@pytest.fixture(autouse=True)
def runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime process: registry root without the declaration, no config root exported, no credential."""
    registry = tmp_path / "app"
    registry.mkdir()
    for name in (
        _CONFIG_ROOT_ENV,
        OVERLAY_FILE_ENV,
        "KAFKA_SASL_USERNAME",
        "KAFKA_SASL_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIBASE_PATH", str(registry))


def _startup(app: asgi.BatchIngestApp) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbox.put_nowait({"type": "lifespan.startup"})
    inbox.put_nowait({"type": "lifespan.shutdown"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def run() -> None:
        await app({"type": "lifespan"}, inbox.get, send)

    asyncio.run(run())
    return sent


def test_lifespan_resolves_the_lane_declaration_the_overlay_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted = tmp_path / "mounted"
    _declaration(mounted)
    store = _store(tmp_path, workspace_config_root=str(mounted))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(store))
    app = asgi.create_app()

    sent = _startup(app)

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ], sent
    # The root was named for the bind only: the process environment is as the runtime left it.
    assert _CONFIG_ROOT_ENV not in os.environ


def test_lifespan_without_a_declaration_anywhere_fails_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(store))
    app = asgi.create_app()

    sent = _startup(app)

    assert sent[0]["type"] == "lifespan.startup.failed"
    assert "ci_bus_lanes.yaml" in sent[0]["message"]


def test_lifespan_a_config_root_the_runtime_exports_wins_over_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = tmp_path / "exported"
    _declaration(exported)
    monkeypatch.setenv(_CONFIG_ROOT_ENV, str(exported))
    store = _store(tmp_path, workspace_config_root=str(tmp_path / "absent"))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(store))
    app = asgi.create_app()

    sent = _startup(app)

    assert sent[0]["type"] == "lifespan.startup.complete", sent
