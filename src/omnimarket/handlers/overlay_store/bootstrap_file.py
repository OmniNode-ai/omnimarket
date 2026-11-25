# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host's bootstrap file: the binding names of its store, broker and Infisical, and no setting.

The file is rendered from the intake inputs. It carries exactly the binding inputs below; a setting, a password or any
other name is never written. A missing intake input refuses with its name before anything is rendered.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.overlay_store.store_wiring import KIND_INPUT

STORE_BINDINGS: tuple[str, ...] = (
    KIND_INPUT,
    "ONEX_SETTINGS_STORE_ADDRESS",
    "ONEX_SETTINGS_STORE_PORT",
    "ONEX_SETTINGS_STORE_DATABASE",
    "ONEX_SETTINGS_STORE_KEY_PREFIX",
    "ONEX_SETTINGS_STORE_LOGIN",
)
BROKER_BINDINGS: tuple[str, ...] = (
    "ONEX_BROKER_KIND",
    "ONEX_BROKER_ADDRESS",
    "ONEX_BROKER_LOGIN",
)
INFISICAL_BINDINGS: tuple[str, ...] = (
    "ONEX_INFISICAL_KIND",
    "ONEX_INFISICAL_IDENTITY",
)
BOOTSTRAP_BINDINGS: tuple[str, ...] = (
    *STORE_BINDINGS,
    *BROKER_BINDINGS,
    *INFISICAL_BINDINGS,
)


class BootstrapInputMissingError(ValueError):
    """An intake input the bootstrap file needs is missing; the message is its name."""


def render_bootstrap_file(intake: Mapping[str, str]) -> str:
    """The file's text: one ``NAME=value`` line per binding, in the declared order, and nothing else."""
    lines: list[str] = []
    for name in BOOTSTRAP_BINDINGS:
        value = intake.get(name, "").strip()
        if not value:
            raise BootstrapInputMissingError(f"the intake input {name} is missing")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def parse_bootstrap_file(text: str) -> dict[str, str]:
    """The bindings of a bootstrap file; a name outside the binding set refuses, so a setting cannot ride in it."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in BOOTSTRAP_BINDINGS:
            raise BootstrapInputMissingError(
                f"the bootstrap file holds {name!r}, which is not a binding name"
            )
        values[name] = value.strip()
    return values


__all__ = [
    "BOOTSTRAP_BINDINGS",
    "BootstrapInputMissingError",
    "parse_bootstrap_file",
    "render_bootstrap_file",
]
