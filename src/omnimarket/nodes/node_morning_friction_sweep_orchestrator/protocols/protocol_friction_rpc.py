# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Interface of the existing infra RequestResponseWiring; broker ownership stays there."""

from typing import Protocol


class ProtocolFrictionRpc(Protocol):
    async def send_request(
        self,
        instance_name: str,
        payload: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object]: ...
