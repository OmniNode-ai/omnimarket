# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production adapters of the iLO setup verb (OMN-22870), bound only by the CLI.

The certificate fetch is a bare TLS handshake that sends nothing to the device; what it returns is a
candidate that the setup trusts for nothing until the iLO's own report of its certificate agrees with
it. The store adapter speaks to the lab secret store with the same machine-identity variables the lane
resolver reads, through the create-only store the secret store effect already owns.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
from collections.abc import Callable, Coroutine
from getpass import getpass
from pathlib import Path
from typing import Any, NamedTuple, TypeVar
from uuid import UUID

import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    CONTRACT_PATH,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_ports import (
    SetupStoreError,
)
from omnimarket.nodes.node_secret_store_effect.adapters.handler_infisical_http import (
    HandlerInfisicalHttp,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    http_exchange,
)

# The machine identity the lane resolver reads (names only). The project and the environment are
# declared by the node contract, not read from the shell.
IDENTITY_VARS = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
)
_T = TypeVar("_T")


class SetupStoreTarget(NamedTuple):
    """The contract-declared place of the iLO keys in the lab secret store."""

    folder: str
    project_id: str
    environment: str
    identity_env_file: str


def load_store_target() -> SetupStoreTarget:
    """The store target the node contract declares (``config.secret_*`` and ``identity_env_file``)."""
    config = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["config"]
    values = {
        name: config[name]["default"]
        for name in (
            "secret_folder",
            "secret_project_id",
            "secret_environment",
            "identity_env_file",
        )
    }
    if not all(isinstance(v, str) and v for v in values.values()):
        raise ValueError("the secret store config of the node contract is incomplete")
    if not values["secret_folder"].startswith("/"):
        raise ValueError("secret_folder must be an absolute folder")
    return SetupStoreTarget(
        folder=values["secret_folder"],
        project_id=values["secret_project_id"],
        environment=values["secret_environment"],
        identity_env_file=values["identity_env_file"],
    )


def read_identity(env_file: str, ambient: dict[str, str]) -> dict[str, str]:
    """The identity variables: the operator env file wins, the shell fills what the file lacks.

    Only the names in ``IDENTITY_VARS`` are taken from the file, which also holds unrelated settings.
    """
    found: dict[str, str] = {n: ambient[n] for n in IDENTITY_VARS if n in ambient}
    path = Path(env_file).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        name, sep, raw = line.removeprefix("export ").partition("=")
        name = name.strip()
        if sep and name in IDENTITY_VARS:
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                found[name] = value
    return found


def fetch_certificate_der(address: str, port: int) -> bytes:
    """The DER certificate the iLO presents. A handshake only: no request is sent."""
    pem = ssl.get_server_certificate((address, port), timeout=10.0)
    return ssl.PEM_cert_to_DER_cert(pem)


class InfisicalSetupSecrets:
    """The iLO folder of the lab secret store: key names, a read, and a create-only put."""

    def __init__(
        self, target: SetupStoreTarget, environ: dict[str, str] | None = None
    ) -> None:
        env = read_identity(
            target.identity_env_file, dict(os.environ) if environ is None else environ
        )
        missing = sorted(n for n in IDENTITY_VARS if not env.get(n, "").strip())
        if missing:
            raise SetupStoreError(
                f"the lab secret store identity is not configured; missing {missing}"
            )
        try:
            project = UUID(target.project_id)
        except ValueError:
            raise SetupStoreError("the declared secret project is not a UUID") from None
        self._folder = "/" + target.folder.strip("/")
        self._store = HandlerInfisicalHttp(
            address=env["INFISICAL_ADDR"].strip(),
            project_id=str(project),
            environment=target.environment,
            client_id=SecretStr(env["INFISICAL_CLIENT_ID"].strip()),
            client_secret=SecretStr(env["INFISICAL_CLIENT_SECRET"].strip()),
            exchange=http_exchange,
            timeout_seconds=15.0,
        )

    @staticmethod
    def _run(work: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        try:
            return asyncio.run(work())
        except PermissionError:
            raise SetupStoreError(
                "the lab secret store refused this identity"
            ) from None
        except (ConnectionError, TimeoutError, OSError):
            raise SetupStoreError("the lab secret store did not answer") from None
        except ValueError:
            raise SetupStoreError("the lab secret store rejected the request") from None

    def names(self) -> frozenset[str]:
        keys = self._run(lambda: self._store.list_keys(prefix=self._folder))
        return frozenset(k.rpartition("/")[2] for k in keys)

    def get(self, key: str) -> SecretStr | None:
        value = self._run(lambda: self._store.get_secret(f"{self._folder}/{key}"))
        return SecretStr(value) if value else None

    def create(self, key: str, value: SecretStr) -> bool:
        return self._run(
            lambda: self._store.set_secret(
                f"{self._folder}/{key}", value.get_secret_value()
            )
        )


def prompt_admin_password(admin_user: str) -> SecretStr | None:
    """One hidden prompt for the run. ``None`` when no terminal can ask."""
    if not sys.stdin.isatty():
        return None
    try:
        value = getpass(
            f"iLO administrator password for '{admin_user}' "
            "(input hidden; asked once for every host without a stored one): "
        )
    except (EOFError, KeyboardInterrupt):
        return None
    return SecretStr(value) if value else None


def prompt_address(host: str) -> str | None:
    """A visible prompt for an iLO address the host table does not carry. ``None`` without a terminal."""
    if not sys.stdin.isatty():
        return None
    try:
        value = input(f"iLO address of {host} (the lab host table has none): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return value or None
