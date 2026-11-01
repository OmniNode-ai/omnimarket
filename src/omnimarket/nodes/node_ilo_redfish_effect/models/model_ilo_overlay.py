# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The private host overlay of the iLO 4 Redfish effect (OMN-21835).

The overlay is ``<root>/node_ilo_redfish_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. It carries every deployment fact: iLO addresses, pinned certificates,
the identity each host must show, and the NAMES of the credential keys. It carries no credential
value. This package ships no host, address, serial or fingerprint.

A host row uses the same ``name`` as its row in the lab host table.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloGeneration,
)

_HOST_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_STORE_KEY = r"^[A-Z][A-Z0-9_]{0,127}$"

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=512)]


class ModelIloTlsPin(BaseModel):
    """The one certificate a host's iLO may present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Path of the PEM file, relative to the directory holding the overlay file.
    ca_file: NonEmptyStr
    # SHA-256 of the DER certificate, lower-case hex. The PEM file must hash to the same value.
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("ca_file")
    @classmethod
    def _relative_and_contained(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError(
                "ca_file must be a relative path inside the overlay directory"
            )
        return value


class ModelIloCredentialKeys(BaseModel):
    """Names of the secret-store keys that hold a host's accounts. Never values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_username_key: Annotated[str, Field(pattern=_STORE_KEY)]
    read_password_key: Annotated[str, Field(pattern=_STORE_KEY)]
    write_username_key: Annotated[str | None, Field(pattern=_STORE_KEY)] = None
    write_password_key: Annotated[str | None, Field(pattern=_STORE_KEY)] = None


class ModelIloHost(BaseModel):
    """One managed server's iLO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=_HOST_NAME)]
    generation: EnumIloGeneration
    # The iLO's address or DNS name. Private: only ever read from the overlay.
    address: NonEmptyStr
    port: Annotated[int, Field(ge=1, le=65535)] = 443
    # A substring the system's Model must contain, for example the product line and generation.
    expected_product: NonEmptyStr
    # The serial the system must report. Absent until the operator has read it from the first read:
    # while absent every write is refused, reads still run and report the live serial.
    expected_serial: NonEmptyStr | None = None
    tls: ModelIloTlsPin
    # Absent: the key names follow the contract's template.
    credentials: ModelIloCredentialKeys | None = None
    # False until the operator has added the account and approved the first read.
    live: bool = False
    # A virtual-media image URL must start with one of these. None allowed while empty.
    media_url_prefixes: tuple[
        Annotated[str, Field(pattern=r"^https?://\S+$")], ...
    ] = ()


class ModelIloOverlay(BaseModel):
    """The document the overlay file holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: Annotated[tuple[ModelIloHost, ...], Field(min_length=1)]
    # Who may be named by ``approved_by`` on an approval row. A consent row without that cell is the
    # operator's own, so the default ``operator`` matches a plain consent row.
    approvers: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)] = ("operator",)
    approval_max_age_hours: Annotated[int, Field(ge=1, le=168)] = 24
    # Directory of the per-host action journals. Private and writable by the caller only.
    journal_dir: NonEmptyStr

    @field_validator("hosts")
    @classmethod
    def _names_unique(cls, hosts: tuple[ModelIloHost, ...]) -> tuple[ModelIloHost, ...]:
        names = [h.name for h in hosts]
        if len(set(names)) != len(names):
            raise ValueError("host names must be unique")
        return hosts

    def host(self, name: str) -> ModelIloHost | None:
        for candidate in self.hosts:
            if candidate.name == name:
                return candidate
        return None


_TEMPLATE_FIELDS = re.compile(r"\{(HOST|ROLE|FIELD)\}")


def credential_key(template: str, *, host: str, role: str, field: str) -> str:
    """Expand the contract's key template: ``{HOST}``, ``{ROLE}``, ``{FIELD}``, upper-cased."""
    values = {"HOST": host, "ROLE": role, "FIELD": field}
    key = _TEMPLATE_FIELDS.sub(
        lambda m: re.sub(r"[^A-Za-z0-9]", "_", values[m.group(1)]).upper(), template
    )
    if re.fullmatch(_STORE_KEY, key) is None:
        raise ValueError("credential key template does not yield a store key name")
    return key
