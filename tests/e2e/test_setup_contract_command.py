# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-instantiated effects must redact stdin without constructor secrets."""

import importlib
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)

pytestmark = pytest.mark.e2e


def test_bound_command_redacts_its_own_secret_stdin(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    contract = yaml.safe_load(
        (root / "src/omnimarket/nodes/node_onex_setup_effect/contract.yaml").read_text()
    )
    declaration = next(
        item
        for item in contract["handler_routing"]["handlers"]
        if item["operation"] == "onex_setup.command"
    )["handler"]
    handler = getattr(
        importlib.import_module(declaration["module"]), declaration["name"]
    )()
    result = handler.handle(
        ModelSetupCommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; key=sys.stdin.read(); print(key); print(key,file=sys.stderr)",
            ),
            cwd=tmp_path,
            stdin=SecretStr("credential-SENTINEL"),
        )
    )
    assert result.returncode == 0
    assert "[redacted]" in result.stdout
    assert "[redacted]" in result.stderr
    assert "SENTINEL" not in result.model_dump_json()
