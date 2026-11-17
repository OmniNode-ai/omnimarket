# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime routing and lazy dependency resolution for the dev signed-in read."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
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

from ._support import CONTRACT_PATH, overlay_document

pytestmark = pytest.mark.unit


def test_contract_routes_the_declared_command() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    topics = contract["topics"]
    assert contract["event_bus"]["subscribe_topics"] == [topics["command"]]
    assert contract["event_bus"]["publish_topics"] == [
        topics["completed"],
        topics["failed"],
    ]
    assert contract["runtime_dispatch"]["command_topic"] == topics["command"]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": topics["completed"],
        "failure": topics["failed"],
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert len(routing["handlers"]) == 1
    route = routing["handlers"][0]
    assert route["operation"] == "dev-signed-in-read.run"
    assert route["message_category"] == "command"
    assert route["handler"]["name"] == "HandlerDevSignedInRead"


def test_no_arg_construction_does_not_resolve_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_resolution(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("construction must not resolve environment or network dependencies")

    for name in (
        "load_overlay",
        "UrllibIdentityTransport",
        "SecretStoreSettingsReader",
    ):
        monkeypatch.setattr(handler_dev_signed_in_read, name, unexpected_resolution)
    HandlerDevSignedInRead()


async def test_no_arg_handle_loads_and_caches_overlay_from_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = overlay_document()
    document["secret_store"] = None
    # Refuse this realm before reading the host's secret store or calling HTTP.
    document["keycloak"]["realm"] = "outside-dev"
    node_dir = tmp_path / "node_dev_signed_in_read_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    handler = HandlerDevSignedInRead()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    command = ModelDevSignedInReadCommand(lane="dev", write=False)
    result = await handler.handle(command)
    assert result.outcome is EnumDevSignedInReadOutcome.REFUSED_NOT_DEV
    assert result.realm == "outside-dev"

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "missing"))
    assert await handler.handle(command) == result


async def test_no_arg_handle_propagates_missing_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "missing"))
    handler = HandlerDevSignedInRead()
    with pytest.raises(NodeOverlayError):
        await handler.handle(ModelDevSignedInReadCommand(lane="dev", write=False))
