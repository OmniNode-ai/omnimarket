# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect's contract: topics, handler, overlay (OMN-24027)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    load_command_topic,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
    load_overlay,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    NODE_DIR,
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

CMD_TOPIC = "onex.cmd.omnimarket.branch-deploy-command-requested.v1"
EVT_TOPIC = "onex.evt.omnimarket.branch-deploy-command-completed.v1"
DEPLOY_TOPIC = "onex.cmd.deploy.rebuild-requested.v1"

OVERLAYS_MAIN_LEGACY = Path(__file__).parent / "overlays_main_legacy.yaml"


def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_topics_and_terminal_event() -> None:
    doc = contract()
    assert doc["event_bus"] == {
        "subscribe_topics": [CMD_TOPIC],
        "publish_topics": [EVT_TOPIC, DEPLOY_TOPIC],
    }
    assert doc["terminal_event"] == EVT_TOPIC
    assert doc["externally_consumed_topics"] == [EVT_TOPIC, DEPLOY_TOPIC]
    assert doc["node_type"] == "effect"
    assert doc["descriptor"]["purity"] == "impure"
    assert doc["descriptor"]["runtime_profiles"] == ["effects"]
    assert doc["descriptor"]["side_effects"] == ["bus-publish"]


def test_handler_and_models_resolve() -> None:
    doc = contract()
    handler = doc["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])


def test_every_setting_is_in_the_overlay_with_no_default() -> None:
    required = {
        f
        for f, info in ModelBranchDeployCommandOverlay.model_fields.items()
        if info.is_required()
    }
    assert required == {"scope", "build_source", "detail_max_chars"}
    assert set(ModelBranchDeployCommandOverlay.model_fields) - required == {
        "hmac_secret_env",
        "agent_health_url",
        "agent_health_timeout_seconds",
        "agent_key_check_field",
        "trigger_argv_prefix",
        "env_file",
        "timeout_seconds",
        "refused_marker",
    }
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in required & set(document):
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelBranchDeployCommandOverlay.model_validate(trimmed)


def _overlays_main_legacy_document() -> dict[str, Any]:
    return yaml.safe_load(OVERLAYS_MAIN_LEGACY.read_text())


async def test_overlays_main_legacy_document_loads_and_refuses_signing() -> None:
    legacy = ModelBranchDeployCommandOverlay.model_validate(
        _overlays_main_legacy_document()
    )
    assert legacy.hmac_secret_env is None
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(
        event_bus=bus,
        overlay=legacy,
        environ={SIGNING_ENV: SIGNING_KEY},
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert "hmac_secret_env is absent" in result.detail
    assert bus.published == []


def test_overlays_main_legacy_document_with_unknown_key_refuses() -> None:
    document = _overlays_main_legacy_document()
    document["not_a_key"] = "no such setting"
    with pytest.raises(ValidationError):
        ModelBranchDeployCommandOverlay.model_validate(document)


def test_overlay_loads_from_the_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_branch_deploy_command_effect"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_overlay() == overlay()


def test_overlay_with_no_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(ValueError, match="node_branch_deploy_command_effect"):
        load_overlay()


def test_contract_secret_dependencies_and_command_topic() -> None:
    doc = contract()
    assert doc["secret_dependencies"]["DEPLOY_AGENT_HMAC_SECRET"]["required"] is True
    assert load_command_topic() == DEPLOY_TOPIC
