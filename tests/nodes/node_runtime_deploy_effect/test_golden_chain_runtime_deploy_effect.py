# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the runtime deploy effect node, over the in-memory bus."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_runtime_deploy_effect.handlers.handler_runtime_deploy import (
    HandlerRuntimeDeploy,
    contract_topics,
)
from omnimarket.nodes.node_runtime_deploy_effect.handlers.handler_runtime_deploy_cli import (
    dispatch,
)
from omnimarket.nodes.node_runtime_deploy_effect.models.model_runtime_deploy_request import (
    ModelRuntimeDeployOptions,
    ModelRuntimeDeployRequest,
)

pytestmark = pytest.mark.unit


def _checkout(root: Path) -> Path:
    (root / "scripts/runtime_build").mkdir(parents=True)
    driver = root / "src/omnibase_infra/handlers/handler_runtime_deploy.sh"
    driver.parent.mkdir(parents=True)
    driver.write_text(
        'main() { echo "driver:$ONEX_DEPLOY_REPOSITORY_ROOT:$*"; return 3; }\n'
    )
    return root


def test_golden_chain_command_to_terminal_carries_the_drivers_streams(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path / "checkout")
    request = ModelRuntimeDeployRequest(
        repository_root=root,
        options=ModelRuntimeDeployOptions(restart=True),
        correlation_id=uuid4(),
    )
    result = asyncio.run(dispatch(request, environment={"PATH": "/usr/bin:/bin"}))
    assert result.correlation_id == request.correlation_id
    assert result.returncode == 3
    assert result.stdout.strip() == f"driver:{root}:--restart"


def test_golden_chain_the_contract_names_the_command_and_terminal_topics() -> None:
    command_topic, terminal_topic = contract_topics()
    assert command_topic == "onex.cmd.omnimarket.runtime-deploy-requested.v1"
    assert terminal_topic == "onex.evt.omnimarket.runtime-deploy-completed.v1"


def test_error_chain_a_root_without_the_build_scripts_is_refused(
    tmp_path: Path,
) -> None:
    request = ModelRuntimeDeployRequest(
        repository_root=tmp_path, correlation_id=uuid4()
    )
    with pytest.raises(ValueError, match="omnibase_infra checkout"):
        asyncio.run(HandlerRuntimeDeploy(environment={}).handle(request))


def test_error_chain_a_root_without_the_driver_is_refused(tmp_path: Path) -> None:
    (tmp_path / "scripts/runtime_build").mkdir(parents=True)
    request = ModelRuntimeDeployRequest(
        repository_root=tmp_path, correlation_id=uuid4()
    )
    with pytest.raises(ValueError, match="runtime deployment handler resource"):
        asyncio.run(HandlerRuntimeDeploy(environment={}).handle(request))
