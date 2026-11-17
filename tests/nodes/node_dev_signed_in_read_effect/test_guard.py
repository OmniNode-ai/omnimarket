# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev guard runs before any secret read or HTTP call, and a missing overlay is a typed failed result.

The contract header promises refusal "before any secret read or HTTP call". These tests hold a real ``secret_store``
block in the overlay and patch the transport and the secret store reader so that constructing either fails the test:
a refusal that built them first would fail here. A missing overlay returns the typed ``overlay_unavailable`` result
instead of raising out of ``handle``, so the runtime emits the handler's own failed event.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml

from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    handler_dev_signed_in_read,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.handlers.handler_dev_signed_in_read import (
    HandlerDevSignedInRead,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models.model_dev_signed_in_read import (
    EnumDevSignedInReadOutcome,
    ModelDevSignedInReadCommand,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.overlay import (
    ModelDevSignedInReadOverlay,
)

from ._support import CONTRACT_PATH, overlay_document
from .test_runtime_route import OVERLAY_ROOTS_ENV, SECRET_STORE_BLOCK, _write_overlay

pytestmark = pytest.mark.unit


def _fail_on_construct(monkeypatch: pytest.MonkeyPatch) -> None:
    def constructed(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("the transport or the secret store was built before the dev guard")

    monkeypatch.setattr(
        handler_dev_signed_in_read, "UrllibIdentityTransport", constructed
    )
    monkeypatch.setattr(
        handler_dev_signed_in_read, "SecretStoreSettingsReader", constructed
    )


def _with_real_store(document: dict[str, Any]) -> None:
    document["secret_store"] = dict(SECRET_STORE_BLOCK)


def test_dev_signed_in_read_guard_refuses_a_foreign_realm_before_building_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def foreign_realm(document: dict[str, Any]) -> None:
        _with_real_store(document)
        document["keycloak"]["realm"] = "outside-dev"

    roots = _write_overlay(tmp_path / "roots", foreign_realm)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(roots))
    _fail_on_construct(monkeypatch)

    import asyncio

    result = asyncio.run(
        HandlerDevSignedInRead().handle(
            ModelDevSignedInReadCommand(lane="dev", write=True)
        )
    )

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert result.realm == "outside-dev"


async def test_dev_signed_in_read_guard_refuses_a_foreign_command_lane_before_building_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = _write_overlay(tmp_path / "roots", _with_real_store)
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(roots))
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="prod", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert "command lane" in result.detail


async def test_dev_signed_in_read_guard_refuses_a_foreign_overlay_lane_before_building_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = overlay_document()
    _with_real_store(document)
    document["lane"] = "prod"
    overlay = ModelDevSignedInReadOverlay.model_validate(document)
    assert overlay.secret_store is not None
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead(
        overlay=overlay, contract_path=CONTRACT_PATH
    ).handle(ModelDevSignedInReadCommand(lane="dev", write=True))

    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert "overlay lane" in result.detail


async def test_dev_signed_in_read_guard_missing_overlay_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "missing"))
    _fail_on_construct(monkeypatch)

    # Red on a raise: ``NodeOverlayError`` escaping ``handle`` fails this call.
    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE
    assert result.ok is False
    assert result.lane == "dev"
    assert result.realm is None
    assert "overlay" in result.detail


async def test_dev_signed_in_read_guard_invalid_overlay_returns_a_typed_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node_dir = tmp_path / "roots" / "node_dev_signed_in_read_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump({"lane": "dev"}), encoding="utf-8"
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "roots"))
    _fail_on_construct(monkeypatch)

    result = await HandlerDevSignedInRead().handle(
        ModelDevSignedInReadCommand(lane="dev", write=True)
    )

    assert result.outcome is EnumDevSignedInReadOutcome.OVERLAY_UNAVAILABLE


def test_dev_signed_in_read_guard_first_resolution_builds_once_under_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[object] = []
    stores: list[object] = []
    gate = threading.Barrier(8)

    def make_transport() -> object:
        transports.append(object())
        return transports[-1]

    def make_store(**_: object) -> object:
        stores.append(object())
        return stores[-1]

    monkeypatch.setattr(
        handler_dev_signed_in_read, "UrllibIdentityTransport", make_transport
    )
    monkeypatch.setattr(
        handler_dev_signed_in_read, "SecretStoreSettingsReader", make_store
    )
    overlay = ModelDevSignedInReadOverlay.model_validate(overlay_document())
    handler = HandlerDevSignedInRead(overlay=overlay)

    def resolve() -> None:
        gate.wait()
        handler._resolve_io(overlay)

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(transports) == 1
    assert len(stores) == 1
