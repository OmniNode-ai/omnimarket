# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_reading_effect: every setting of the host readings, none in code.

Found at ``<root>/node_host_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_host_reading import EnumHostProbe
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_host_reading_effect"


class ModelHostReadingPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelOutboxProbeSettings(BaseModel):
    """Where a host's queued ledger rows sit and when they are too old or refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path_env: str = Field(min_length=1)
    lock_dirname: str = Field(min_length=1)
    outbox_suffix: str = Field(min_length=1)
    refused_dirname: str = Field(min_length=1)
    pending_glob: str = Field(min_length=1)
    queued_at_field: str = Field(min_length=1)
    oldest_max_seconds: int = Field(ge=0)
    refused_max: int = Field(ge=0)
    injected_fault: str = Field(min_length=1)


class ModelOldNameLink(BaseModel):
    """One old ledger name and the entry beside the configured ledger it must resolve to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    ledger_relative: str = Field(min_length=1)


class ModelOldNamesProbeSettings(BaseModel):
    """The old names kept as links during the move; removed with the probe at the bus cutover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    ledger_path_env: str = Field(min_length=1)
    links: tuple[ModelOldNameLink, ...] = Field(min_length=1)
    injected_fault: str = Field(min_length=1)


class ModelHostReadingHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    probes: tuple[EnumHostProbe, ...] = Field(min_length=1)
    #: Environment the host's installed unit carries (the routes and their roots).
    environment: dict[str, str]


class ModelHostReadingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append: ModelLedgerAppendRoute
    publish: ModelHostReadingPublish
    outbox: ModelOutboxProbeSettings
    old_names: ModelOldNamesProbeSettings | None
    hosts: tuple[ModelHostReadingHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelHostReadingOverlay:
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a host is declared twice")
        for host in self.hosts:
            if len(host.probes) != len(set(host.probes)):
                raise ValueError(f"host {host.name} declares a probe twice")
            if EnumHostProbe.LEDGER_OLD_NAMES in host.probes and not self.old_names:
                raise ValueError(
                    f"host {host.name} declares ledger-old-names with no old_names settings"
                )
        return self

    def host(self, name: str) -> ModelHostReadingHost | None:
        return next((host for host in self.hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the readings depend on, so a threshold change shows in the row."""
        text = json.dumps(
            {
                "outbox": self.outbox.model_dump(mode="json"),
                "old_names": self.old_names.model_dump(mode="json")
                if self.old_names
                else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHostReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostReadingOverlay, environ)
