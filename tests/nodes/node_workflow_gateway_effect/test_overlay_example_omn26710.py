# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway ships an example settings document, and the cloud runtime loads settings from a mount.

Exemplar: tests/nodes/node_workflow_gateway_effect/test_error_chain_workflow_gateway.py
(test_no_overlay_document_answers_503_on_every_route), which starts the mount with a settings folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_dependencies import (
    OVERLAY_ID,
    load_contract_config,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_gateway_overlay import (
    ModelWorkflowGatewayOverlay,
)

NODE = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_workflow_gateway_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"


def _example() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_example_validates_against_the_settings_model() -> None:
    overlay = ModelWorkflowGatewayOverlay.model_validate(_example())
    assert overlay.identity.tenant_id_claim == "tenant_id"
    assert overlay.identity.tenant_slug_claim == "tenant_slug"
    assert overlay.api_key.header
    assert overlay.api_key.prefix
    assert overlay.rate_limit.refill_per_minute >= 1
    topics = {w.command_topic for w in overlay.catalog.workflows}
    assert all(t.startswith("onex.cmd.") for t in topics)


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(lambda d: d["identity"].pop("tenant_slug_claim"), id="no-slug"),
        pytest.param(lambda d: d["identity"].update(jwks_url="keys"), id="bad-url"),
        pytest.param(lambda d: d.pop("rate_limit"), id="no-rate-limit"),
    ],
)
def test_a_broken_example_is_refused(break_it: Any) -> None:
    document = _example()
    break_it(document)
    with pytest.raises(ValidationError):
        ModelWorkflowGatewayOverlay.model_validate(document)


def test_contract_declares_the_settings_mount() -> None:
    mount = load_contract_config()["settings_mount"]
    assert Path(mount["path"]).is_absolute()


async def _status(app_root: Path | None, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    app = create_app(settings_mount=app_root)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime"
    ) as client:
        answer = await client.get("/v1/workflows/contracts")
    return answer.status_code


async def test_settings_from_the_mount_configure_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / OVERLAY_ID
    folder.mkdir()
    (folder / "overlay.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"))
    # Configured: the request reaches authentication and is refused there, not with 503.
    assert await _status(tmp_path, monkeypatch) == 401


async def test_an_empty_mount_still_answers_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _status(tmp_path, monkeypatch) == 503
