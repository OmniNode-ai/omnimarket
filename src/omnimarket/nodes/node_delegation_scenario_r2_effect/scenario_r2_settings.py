# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R2's settings from the secret store, through the machine's identity (OMN-23432).

The machine's own overlay is asked first; a machine that names no store is asked through the
identity its host env file carries. A value is returned only when the store answered resolved
with a non-empty value, and a store that raises, or answers otherwise, supplies nothing for that
name. Values are ``SecretStr`` and move in process only: nothing here logs, prints, or returns
a value but the environment dict ``fill_settings`` returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    ModelScenarioR2Runtime,
)
from omnimarket.nodes.node_secret_store_effect.adapters import IdentityFileStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HOST_ENV_FILE,
    host_env_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)


class ProtocolSettingStore(Protocol):
    """The store seam the tick asks its settings of: one name at a time, in process."""

    async def read(self, folder: str, name: str) -> SecretStr | None: ...


class SecretStoreSettingsReader:
    """The real store seam: the machine's overlay first, the host env file's identity second.

    Args:
        onex_home: The ONEX home holding ``config.yaml``; defaults to the standard one.
        home: The user's home holding ``.omnibase/.env``; defaults to ``Path.home()``.
    """

    def __init__(
        self, *, onex_home: Path | None = None, home: Path | None = None
    ) -> None:
        self._onex_home = onex_home
        self._home = home

    async def read(self, folder: str, name: str) -> SecretStr | None:
        request = ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            folder=folder,
            key=name,
        )
        result = await HandlerSecretStore(onex_home=self._onex_home).handle(request)
        if result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED:
            home = self._home or Path.home()
            overlay = host_env_overlay(home)
            if overlay is None:
                return None
            result = await HandlerSecretStore(
                overlay=overlay,
                onex_home=self._onex_home,
                bootstrap_store=IdentityFileStore(home / HOST_ENV_FILE),
            ).handle(request)
        if result.outcome is not EnumSecretStoreOutcome.RESOLVED:
            return None
        value = result.value
        if value is None or not value.get_secret_value():
            return None
        return value


def setting_names(runtime: ModelScenarioR2Runtime) -> tuple[str, ...]:
    """The seven setting variables the runtime declares, in the order it declares them."""
    onboarding = runtime.onboarding
    return (
        onboarding.provider_env,
        onboarding.provider_url_env,
        onboarding.key_env,
        onboarding.model_env,
        runtime.local_first_env,
        runtime.local_second_env,
        runtime.dead_url_env,
    )


async def fill_settings(
    environment: Mapping[str, str],
    runtime: ModelScenarioR2Runtime,
    store: ProtocolSettingStore,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The environment with every absent or blank setting filled from the store.

    Returns the filled environment and, in declared order, the names the store did not supply.
    A store that raises supplies nothing for that name and the filling keeps going.
    """
    filled: dict[str, str] = dict(environment)
    folder = runtime.secret_store_folder
    unresolved: list[str] = []
    for name in setting_names(runtime):
        if filled.get(name, "").strip():
            continue
        try:
            secret = await store.read(folder, name)
        except Exception:
            secret = None
        value = secret.get_secret_value() if secret is not None else ""
        if value.strip():
            filled[name] = value
        else:
            unresolved.append(name)
    return filled, tuple(unresolved)


__all__ = [
    "ProtocolSettingStore",
    "SecretStoreSettingsReader",
    "fill_settings",
    "setting_names",
]
