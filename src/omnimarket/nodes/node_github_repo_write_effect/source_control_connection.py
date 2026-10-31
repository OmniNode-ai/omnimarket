# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open connection behind the GitHub source-control adapter (OMN-21536).

The adapter, ``HandlerSourceControlGithub``, answers the omnibase_spi source-control
protocols, whose ``connect`` and ``close`` open and drop a connection. The
connection (the transport and the repo-write effect built on it) is state, so it
lives here and the adapter is given it: no public adapter method assigns to the
adapter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolGithubRepoWriteTransport,
)

__all__: list[str] = ["SourceControlConnection"]

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"


class SourceControlConnection:
    """The transport and write effect of one source-control session.

    ``transport`` is injected in tests (a recorded fake); without one, :meth:`open`
    builds the live transport from the contract-declared ``GITHUB_TOKEN`` ref.
    """

    def __init__(
        self,
        transport: ProtocolGithubRepoWriteTransport | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._writer: HandlerGithubRepoWriteEffect | None = None

    @property
    def transport(self) -> ProtocolGithubRepoWriteTransport | None:
        """The open transport, or None before :meth:`open` and after :meth:`release`."""
        return self._transport

    @property
    def writer(self) -> HandlerGithubRepoWriteEffect | None:
        """The write effect built on the open transport, or None when not open."""
        return self._writer

    async def open(self) -> bool:
        """Open the connection; False when no GitHub credential resolves."""
        if self._transport is None:
            ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
            secret = await resolve_api_key_async(ref, env_var_fallback=ref)
            if secret is None or not secret.get_secret_value():
                return False
            self._transport = UrllibGithubLandingTransport(secret)
        self._writer = HandlerGithubRepoWriteEffect(self._transport, clock=self._clock)
        return True

    def release(self) -> None:
        """Drop the transport and the write effect."""
        self._transport = None
        self._writer = None
