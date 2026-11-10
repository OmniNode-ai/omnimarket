# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation scenario settings found under the names that already exist (OMN-23602).

A scenario's settings overlay names, for each run variable, an existing secret (or a template of existing names, or a
literal). Each name is looked up in the process environment, then the host environment file (only the names asked for are
read from it), then, when a store is given, the store folder through the machine's identity. A looked-up value moves
in process only and lands only in the returned environment copy: nothing here logs or prints one, and the unresolved result
holds variable names, never values.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import SecretStr

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioSettingsOverlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
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


def load_settings_overlay(path: Path) -> ModelScenarioSettingsOverlay:
    """The settings overlay file at ``path``, validated."""
    return ModelScenarioSettingsOverlay.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def read_host_env_names(
    home: Path, relpath: str, wanted: frozenset[str]
) -> dict[str, str]:
    """The non-blank values of the ``wanted`` names in the host environment file; no other name is returned."""
    found: dict[str, str] = {}
    try:
        lines = (home / relpath).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return found
    for line in lines:
        name, sep, raw = line.strip().removeprefix("export ").partition("=")
        name = name.strip()
        if not sep or name not in wanted:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value.strip():
            found[name] = value
    return found


async def _store_values(
    store: ProtocolSettingStore, folder: str, names: list[str]
) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in names:
        try:
            secret = await store.read(folder, name)
        except Exception:
            continue
        value = secret.get_secret_value() if secret is not None else ""
        if value.strip():
            found[name] = value
    return found


def fill_declared_settings(
    environment: Mapping[str, str],
    overlay: ModelScenarioSettingsOverlay,
    *,
    home: Path,
    store: ProtocolSettingStore | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """A copy of ``environment`` with each overlay setting the environment does not hold filled, and the names left unfilled.

    A setting the environment already holds is left as it is and nothing is looked up for it. Every other setting reads its
    existing names from the process environment, then the host environment file, then the store (only when one is given;
    a store that raises supplies nothing for that name). The unresolved names are in overlay order.
    """
    filled: dict[str, str] = dict(environment)
    pending = [s for s in overlay.settings if not filled.get(s.env, "").strip()]
    needed = list(dict.fromkeys(n for s in pending for n in s.source_names()))
    found: dict[str, str] = {
        n: environment[n] for n in needed if environment.get(n, "").strip()
    }
    missing = [n for n in needed if n not in found]
    if missing:
        found.update(
            read_host_env_names(home, overlay.host_env_file, frozenset(missing))
        )
        missing = [n for n in missing if n not in found]
    if missing and store is not None:
        found.update(asyncio.run(_store_values(store, overlay.folder, missing)))
    unresolved: list[str] = []
    for setting in pending:
        value = setting.render(found)
        if value is None:
            unresolved.append(setting.env)
        else:
            filled[setting.env] = value
    return filled, tuple(unresolved)


def fill_overlay_settings(
    environment: dict[str, str],
    relpath: str | None,
    *,
    node_dir: Path,
    store: ProtocolSettingStore | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """``environment`` filled from the node's overlay at ``node_dir / relpath``; unchanged when the node declares none."""
    if relpath is None:
        return environment
    overlay = load_settings_overlay(node_dir / relpath)
    filled, _ = fill_declared_settings(
        environment, overlay, home=home or Path.home(), store=store
    )
    return filled


__all__ = [
    "ProtocolSettingStore",
    "SecretStoreSettingsReader",
    "fill_declared_settings",
    "fill_overlay_settings",
    "load_settings_overlay",
    "read_host_env_names",
]
