# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B effect handler owning the native host deployment implementation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import yaml

from ..models.model_runtime_deploy_request import ModelRuntimeDeployRequest
from ..models.model_runtime_deploy_result import ModelRuntimeDeployResult

CONTRACT_PATH = Path(__file__).parents[1] / "contract.yaml"


def contract_topics() -> tuple[str, str]:
    """Read the single command/terminal route from its owning contract."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    commands = contract["event_bus"]["subscribe_topics"]
    terminals = contract["event_bus"]["publish_topics"]
    if len(commands) != 1 or len(terminals) != 1:
        raise ValueError(
            "runtime deployment requires one command and one terminal topic"
        )
    return str(commands[0]), str(terminals[0])


class HandlerRuntimeDeploy:
    """Execute on the host owning the source checkout and Docker daemon."""

    def __init__(self, *, environment: dict[str, str] | None = None) -> None:
        self._environment = dict(os.environ if environment is None else environment)

    async def handle(
        self, request: ModelRuntimeDeployRequest
    ) -> ModelRuntimeDeployResult:
        if not (request.repository_root / "scripts/runtime_build").is_dir():
            raise ValueError("repository_root must name the omnibase_infra checkout")
        driver_path = (
            request.repository_root
            / "src/omnibase_infra/handlers/handler_runtime_deploy.sh"
        )
        if not driver_path.is_file():
            raise ValueError(
                "repository_root has no native runtime deployment handler resource"
            )
        env = {
            **self._environment,
            "ONEX_DEPLOY_REPOSITORY_ROOT": str(request.repository_root),
        }
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            'source "$1"; shift; main "$@"',
            "onex-runtime-deploy",
            str(driver_path),
            *request.options.to_arguments(),
            env=env,
            cwd=request.repository_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            # Give the deployment's EXIT trap the opportunity to release locks
            # and restore its host state before the handler itself is abandoned.
            process.terminate()
            await process.communicate()
            raise
        return ModelRuntimeDeployResult(
            correlation_id=request.correlation_id,
            repository_root=request.repository_root,
            returncode=process.returncode if process.returncode is not None else 1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
