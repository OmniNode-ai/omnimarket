# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the operator access mode effect node."""

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_access_mode_detect import (
    HandlerAccessModeDetect,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_foreground_bash_guard import (
    HandlerForegroundBashGuard,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.enum_access_mode import (
    EnumAccessMode,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_access_mode_request import (
    ModelAccessModeRequest,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_foreground_bash_request import (
    ModelForegroundBashRequest,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _detect(name: str, state: Path) -> EnumAccessMode:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    environ = {**loaded["environ"], "OMNI_ACCESS_MODE_STATE_DIR": str(state)}
    request = ModelAccessModeRequest(loaded["payload"]["session_id"], state, environ)
    return HandlerAccessModeDetect().handle(request).mode


def test_golden_chain_session_environment_to_access_mode(tmp_path: Path) -> None:
    assert _detect("local_cli", tmp_path) is EnumAccessMode.LOCAL
    assert _detect("remote_bridge", tmp_path) is EnumAccessMode.REMOTE
    assert _detect("headless_sdk", tmp_path) is EnumAccessMode.HEADLESS


def test_golden_chain_foreground_bash_allows_a_plain_command() -> None:
    verdict = HandlerForegroundBashGuard().handle(
        ModelForegroundBashRequest(
            command="ls -la", main_session=True, operator_facing=True
        )
    )
    assert verdict.allowed
    assert verdict.reason == ""


def test_error_chain_foreground_wait_is_refused_with_its_guard_and_reason() -> None:
    verdict = HandlerForegroundBashGuard().handle(
        ModelForegroundBashRequest(
            command="sleep 300", main_session=True, operator_facing=True
        )
    )
    assert not verdict.allowed
    assert verdict.reason
    assert verdict.guard.startswith("operator_access_mode_")
    assert verdict.reason_class
