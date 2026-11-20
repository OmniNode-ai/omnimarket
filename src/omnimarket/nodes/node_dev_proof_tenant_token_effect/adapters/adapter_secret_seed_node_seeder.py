# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seeder adapter: omnibase_infra's ``node_secret_seed_effect`` handler, the only write path to the store.

The seed node reads a dotenv-style source; this adapter hands it the one ``NAME=value`` line through the node's
injectable source reader, so the value sits in memory for the call and never on disk or in a request. A name the seed
node's dry run lists as already present is never written: the seed node upserts, so this adapter asks first.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from omnibase_infra.nodes.node_secret_seed_effect.handlers.handler_secret_seed import (
    HandlerSecretSeed,
    TypeAuthProbe,
    TypeStoreFactory,
)
from omnibase_infra.nodes.node_secret_seed_effect.models.enum_secret_seed_verdict import (
    EnumSecretSeedVerdict,
)
from omnibase_infra.nodes.node_secret_seed_effect.models.model_secret_seed_request import (
    ModelSecretSeedRequest,
)
from omnibase_infra.nodes.node_secret_seed_effect.models.model_secret_seed_result import (
    ModelSecretSeedResult,
)
from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenStatus,
)

_IN_PROCESS_SOURCE = "in-process"
_PLACEHOLDER = "placeholder"


class SeedNodeError(RuntimeError):
    """The seed node did not answer a plan or a write; the message names the verdict, never a value."""


class SecretSeedNodeSeeder:
    """``ProtocolTokenSeeder`` over ``HandlerSecretSeed``."""

    def __init__(
        self,
        *,
        host: str,
        project_id: UUID,
        environment: str,
        folder: str,
        store_factory: TypeStoreFactory | None = None,
        auth_probe: TypeAuthProbe | None = None,
    ) -> None:
        self._host = host
        self._project_id = project_id
        self._environment = environment
        self._folder = folder
        self._store_factory = store_factory
        self._auth_probe = auth_probe

    def _request(self, name: str, *, execute: bool) -> ModelSecretSeedRequest:
        return ModelSecretSeedRequest(
            correlation_id=uuid4(),
            source_path=_IN_PROCESS_SOURCE,
            infisical_host=self._host,
            project_id=self._project_id,
            environment_slug=self._environment,
            secret_path=self._folder,
            keys=[name],
            execute=execute,
        )

    async def _plan(self, name: str) -> ModelSecretSeedResult:
        """The seed node's dry run: one write-free pass that lists which names exist."""
        handler = HandlerSecretSeed(
            store_factory=self._store_factory,
            source_reader=lambda _path: f"{name}={_PLACEHOLDER}\n",
            auth_probe=self._auth_probe,
        )
        result = await handler.handle(self._request(name, execute=False))
        if not result.success:
            raise SeedNodeError(f"seed node plan verdict={result.verdict.value}")
        return result

    async def exists(self, name: str) -> bool:
        return name in (await self._plan(name)).updated_names

    async def seed(self, name: str, value: SecretStr) -> EnumDevProofTokenStatus:
        if await self.exists(name):
            return EnumDevProofTokenStatus.PRESENT
        text = f"{name}={value.get_secret_value()}\n"
        handler = HandlerSecretSeed(
            store_factory=self._store_factory,
            source_reader=lambda _path: text,
            auth_probe=self._auth_probe,
        )
        result = await handler.handle(self._request(name, execute=True))
        if (
            result.verdict is EnumSecretSeedVerdict.SEEDED
            and name in result.created_names
        ):
            return EnumDevProofTokenStatus.WRITTEN
        return EnumDevProofTokenStatus.FAILED


__all__ = ["SecretSeedNodeSeeder", "SeedNodeError"]
