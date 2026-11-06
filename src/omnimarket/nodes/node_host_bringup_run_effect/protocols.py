# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected boundaries of the host bring-up run: the installer and the deploy-store read."""

from typing import Protocol

from pydantic import BaseModel

from omnimarket.nodes.node_host_bringup_run_effect.models import ModelInstallerOutcome


class ProtocolHostInstaller(Protocol):
    """Runs one installer unit in one mode and answers its exit code and output."""

    def run(
        self, unit: str, mode: str, host_name: str | None
    ) -> ModelInstallerOutcome: ...


class ProtocolDeployStoreReader(Protocol):
    """Answers whether the host declares a deploy-agent env store."""

    def has_deploy_agent_store(self, host_name: str) -> bool: ...


class ProtocolEventPublisher(Protocol):
    """Publishes a typed event on a declared topic."""

    def publish(self, topic: str, event: BaseModel) -> None: ...
