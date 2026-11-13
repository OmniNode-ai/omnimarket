# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The App installation-token minter: one fresh token per mint, from named secrets.

The overlay names the App id and the App private key by their secret-store references; this
adapter resolves the two values at mint time and never logs a value or puts one in an error.

Where the two values come from is the overlay's choice (OMN-24311): with no store block the
minter asks the process environment and the lane's resolver as before; with a store block the
minter is built with a reader of that store and a folder, and reads both names from there
through the machine's identity file, with no fallback to the process environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from threading import Thread
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import (
    GitHubAppCredentialMissingError,
    GitHubAppIdentityError,
    mint_installation_token,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key

if TYPE_CHECKING:
    from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore


def _run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to its end from sync code, on a worker thread when this thread has a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    box: list[T] = []
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            box.append(asyncio.run(coroutine))
        except BaseException as error:
            failure.append(error)

    worker = Thread(target=_target, name="exposure-mint-secret-read")
    worker.start()
    worker.join()
    if failure:
        raise failure[0]
    return box[0]


class AppInstallationTokenMint:
    def __init__(
        self,
        *,
        reader: ProtocolSettingStore | None = None,
        folder: str | None = None,
    ) -> None:
        if (reader is None) != (folder is None):
            raise ValueError("a store reader and its folder are given together")
        self._store = None if reader is None or folder is None else (reader, folder)

    def _read_from_store(
        self, reader: ProtocolSettingStore, folder: str, name: str
    ) -> str:
        try:
            secret: SecretStr | None = _run_sync(reader.read(folder, name))
        except Exception as error:
            raise GitHubAppCredentialMissingError(
                f"token mint could not read {name!r} from the secret store folder "
                f"{folder!r}: {type(error).__name__}"
            ) from None
        if secret is None or not secret.get_secret_value():
            raise GitHubAppCredentialMissingError(
                f"token mint requires {name!r} in the secret store folder "
                f"{folder!r}, but the store did not return it"
            )
        return secret.get_secret_value()

    def _read_from_environment(self, name: str) -> str:
        secret = resolve_api_key(name, env_var_fallback=name, required=False)
        if secret is None:
            raise GitHubAppCredentialMissingError(
                f"token mint requires {name!r}, but it did not resolve "
                "from the secret store or environment"
            )
        return secret.get_secret_value()

    def _read(self, name: str) -> str:
        if self._store is not None:
            reader, folder = self._store
            return self._read_from_store(reader, folder, name)
        return self._read_from_environment(name)

    def mint(self, *, org: str, app_id_secret: str, private_key_secret: str) -> str:
        app_id = self._read(app_id_secret)
        private_key = self._read(private_key_secret)
        try:
            return mint_installation_token(
                app_id=app_id,
                private_key_pem=private_key,
                org=org,
                private_key_ref=private_key_secret,
            )
        except GitHubApiError as error:
            raise GitHubAppIdentityError(
                f"App installation-token mint failed for org {org!r}: {error}"
            ) from error
