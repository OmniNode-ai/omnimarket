# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real RPK broker login admin adapter over SSH."""

from __future__ import annotations

import asyncio
from functools import partial

from pydantic import SecretStr

from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.driver_broker_acl import (
    AclConnection,
    RemoteRunner,
    build_remote_script,
    run_remote,
)

from ..models.model_service_principal_mint import ModelServicePrincipalBroker
from ..protocols.protocol_service_principal_mint import (
    BrokerLoginAdminError,
)


class RpkSshBrokerLoginAdmin:
    """Implements ProtocolBrokerLoginAdmin using RPK over SSH."""

    def __init__(
        self,
        broker: ModelServicePrincipalBroker,
        runner: RemoteRunner | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self._broker = broker
        self._runner: RemoteRunner = runner or partial(run_remote, timeout=timeout_s)
        self._timeout_s = timeout_s
        self._connection = AclConnection(
            lane=broker.lane,
            host=broker.host,
            compose_project=broker.compose_project,
            broker_container=broker.broker_container,
            superuser_source_container=broker.superuser_source_container,
            ssh_target=broker.ssh_target,
            timeout_seconds=timeout_s,
        )

    def _run(self, script: str) -> str:
        return self._runner(self._connection.ssh_target, script)

    async def list_logins(self) -> tuple[str, ...]:
        rpk_argvs = [["security", "user", "list"]]
        script = build_remote_script(self._connection, rpk_argvs)
        try:
            stdout = await asyncio.to_thread(self._run, script)
        except Exception as exc:
            raise BrokerLoginAdminError(
                f"list logins failed: {type(exc).__name__}"
            ) from None

        logins: list[str] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            first_token = line.split()[0]
            if first_token.upper() in ("USERNAME", "NAME"):
                continue
            logins.append(first_token)
        return tuple(logins)

    async def create_login(self, name: str, password: SecretStr) -> None:
        rpk_argvs = [
            [
                "security",
                "user",
                "create",
                name,
                "--password",
                password.get_secret_value(),
                "--mechanism",
                self._broker.mechanism,
            ]
        ]
        script = build_remote_script(self._connection, rpk_argvs)
        try:
            await asyncio.to_thread(self._run, script)
        except Exception as exc:
            error_msg = f"create login failed: {type(exc).__name__}"
            if password.get_secret_value() in error_msg:
                error_msg = error_msg.replace(password.get_secret_value(), "***")
            raise BrokerLoginAdminError(error_msg) from None
