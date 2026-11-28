# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings store provision overlay (OMN-24052): which Valkey is the dev store, the three login names, their secret names.

Found at ``<root>/node_settings_store_provision_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. Every
value is a deployment fact and none has a default. Nothing here is a secret: a password is a ``generated_keys`` id of
node_secret_store_effect (its folder and key name are that contract's) and the Valkey admin password is a folder and key name.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.overlay_store.backend_valkey import (
    ADDRESS_INPUT,
    DATABASE_INPUT,
    KEY_PREFIX_INPUT,
    LOGIN_INPUT,
    PORT_INPUT,
)
from omnimarket.handlers.overlay_store.bootstrap_file import BOOTSTRAP_BINDINGS
from omnimarket.handlers.overlay_store.store_wiring import KIND_INPUT
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_settings_store_provision_effect"


class ModelValkeyStoreOverlay(BaseModel):
    """The Valkey the store lives in and the admin login that applies access-list rows to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: int = Field(ge=0)
    key_prefix: str = Field(min_length=1)
    cache_key_pattern: str = Field(min_length=1)
    admin_login: str = Field(min_length=1)
    admin_password_folder: str = Field(min_length=1)
    admin_password_key: str = Field(min_length=1)


class ModelLoginOverlay(BaseModel):
    """One login: its name and the generated_keys id of its password."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    password_key_id: str = Field(min_length=1)


class ModelLoginsOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    writer: ModelLoginOverlay
    reader: ModelLoginOverlay
    cache: ModelLoginOverlay


class ModelSettingsStoreProvisionOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    store: ModelValkeyStoreOverlay
    logins: ModelLoginsOverlay
    secret_store: ModelSecretStoreOverlay | None = None
    #: The eleven bootstrap bindings a host's bootstrap file is rendered from (``onex settings bootstrap-file --intake``): names
    #: and addresses only. The store bindings must equal this document's own store and writer login.
    bootstrap_intake: dict[str, str]

    @model_validator(mode="after")
    def _intake_names_this_store(self) -> ModelSettingsStoreProvisionOverlay:
        intake = self.bootstrap_intake
        if set(intake) != set(BOOTSTRAP_BINDINGS):
            raise ValueError(
                f"bootstrap_intake must hold exactly {sorted(BOOTSTRAP_BINDINGS)}"
            )
        expected = {
            ADDRESS_INPUT: self.store.address,
            PORT_INPUT: str(self.store.port),
            DATABASE_INPUT: str(self.store.database),
            KEY_PREFIX_INPUT: self.store.key_prefix,
            LOGIN_INPUT: self.logins.writer.name,
        }
        for name, value in expected.items():
            if intake[name] != value:
                raise ValueError(
                    f"bootstrap_intake {name} differs from the store this overlay declares"
                )
        if KIND_INPUT not in intake or not all(v.strip() for v in intake.values()):
            raise ValueError("bootstrap_intake holds an empty value")
        return self


def render_intake(overlay: ModelSettingsStoreProvisionOverlay) -> str:
    """The intake file text: one ``NAME=value`` line per binding, in the bootstrap file's declared order."""
    return (
        "\n".join(
            f"{name}={overlay.bootstrap_intake[name]}" for name in BOOTSTRAP_BINDINGS
        )
        + "\n"
    )


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelSettingsStoreProvisionOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(
        NODE_NAME, ModelSettingsStoreProvisionOverlay, environ
    )


__all__ = [
    "NODE_NAME",
    "ModelLoginOverlay",
    "ModelLoginsOverlay",
    "ModelSettingsStoreProvisionOverlay",
    "ModelValkeyStoreOverlay",
    "load_overlay",
    "render_intake",
]
