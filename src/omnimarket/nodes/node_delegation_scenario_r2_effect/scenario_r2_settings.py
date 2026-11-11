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

from omnimarket.handlers.delegation_scenario_settings import (
    ProtocolSettingStore,
    SecretStoreSettingsReader,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    ModelScenarioR2Runtime,
)


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
