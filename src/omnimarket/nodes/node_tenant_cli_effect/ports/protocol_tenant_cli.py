# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three things the ``onex tenant`` handlers reach outside the process through.

The handlers hold no database connection and no broker client: the bus, the overlays and the dev secret store are
reached only through these ports, and the command line builds the real ones.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr


class TenantBusUnavailableError(RuntimeError):
    """The bus could not be opened or refused the write or the read; the message names no credential."""


class TenantOverlayUnavailableError(RuntimeError):
    """An overlay this command reads is missing or lacks the field; the message names the node and the field."""


class ProtocolTenantDispatch(Protocol):
    """Publish one command on its requested topic and read its one answer from the completed topic."""

    def lane(self, node_id: str) -> str | None:
        """The lane the command to ``node_id`` goes out on, from that node's overlay; None when it names none."""
        ...

    def dispatch(
        self,
        *,
        node_id: str,
        command_topic: str,
        response_topic: str,
        payload: dict[str, object],
        correlation_in_payload: bool,
    ) -> dict[str, object] | None:
        """The answer's payload, or None when no answer came inside the wait.

        Raises:
            TenantBusUnavailableError: the bus could not be used.
        """
        ...


class ProtocolGatewayKeyOverlays(Protocol):
    """The deployment values a minted key depends on, read from the nodes' overlays."""

    def gateway_prefix(self) -> str:
        """The prefix the gateway requires every key to start with.

        Raises:
            TenantOverlayUnavailableError: the gateway overlay is missing or declares no prefix.
        """
        ...

    def key_node_lane(self) -> str | None:
        """The ``lane`` of the gateway key node's overlay."""
        ...

    def key_node_dev_store_environment(self) -> str | None:
        """The key node overlay's ``dev_secret_store_environment``; set only in the dev overlay."""
        ...


class ProtocolDevSecretStore(Protocol):
    """The dev secret store's gateway key folder, written create-only in process."""

    def environment(self) -> str | None:
        """The environment this machine's secret store block names; None when it names none."""
        ...

    def create(self, name: str, value: SecretStr) -> str:
        """Write ``gateway_key/<name>`` create-only; the store's outcome word (``created``, ``exists``, ...)."""
        ...


__all__: list[str] = [
    "ProtocolDevSecretStore",
    "ProtocolGatewayKeyOverlays",
    "ProtocolTenantDispatch",
    "TenantBusUnavailableError",
    "TenantOverlayUnavailableError",
]
