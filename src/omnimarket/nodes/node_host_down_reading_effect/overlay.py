# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_down_reading_effect: every setting of the sweep, none in code.

Found at ``<root>/node_host_down_reading_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load
and names it. ``profile`` picks the backend: ``in_memory`` (the default a test and a first run use), ``jsonl`` (a durable
file on one host).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    ModelHostDownRules,
)

NODE_NAME = "node_host_down_reading_effect"
EXAMPLE_OVERLAY = Path(__file__).with_name("overlay.example.yaml")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostDownReadingCodes(_Frozen):
    """The typed failure codes of the sweep chain."""

    observer_not_allowed: str = Field(min_length=1)
    table_unreadable: str = Field(min_length=1)
    write_failed: str = Field(min_length=1)
    readback_failed: str = Field(min_length=1)
    publish_failed: str = Field(min_length=1)


class ModelProbeSettings(_Frozen):
    """The probe of one host: the port it answers on and how long to wait."""

    port: int
    timeout_s: float = Field(gt=0)


class ModelPublishSettings(_Frozen):
    """Where a reading is published: the bus, its lane and the timeout of one publication."""

    bus: Literal["inmemory", "kafka"]
    bus_lane: str = Field(min_length=1)
    kafka_bootstrap: str | None
    timeout_s: float = Field(gt=0)


class ModelEmissionSettings(_Frozen):
    """Where the chain events of the real profiles go: the journal the floor reads its receipts from."""

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)


class ModelHostDownOverlay(_Frozen):
    profile: Literal["in_memory", "jsonl"]
    #: The jsonl profile's state file, relative to the emission home.
    jsonl_state_relpath: str = Field(min_length=1)
    #: The rebuild piece the chain reports under.
    piece: int = Field(ge=1, le=18)
    chain_node: str = Field(min_length=1)
    #: The named break that makes this reading go red, used by the failing control.
    injected_fault: str = Field(min_length=1)
    #: The lab hosts allowed to run a sweep; never the Mac.
    observers: tuple[str, ...] = Field(min_length=1)
    #: The environment variable naming this host's own name.
    observer_env: str = Field(min_length=1)
    probe: ModelProbeSettings
    rules: ModelHostDownRules
    #: A stored reading older than this does not count as down for a reader.
    reading_ttl_s: int = Field(ge=1)
    #: argv printing one line ``<host> <ISO-8601 time>`` per host that advertised capacity; None means no source.
    advertisement_command: tuple[str, ...] | None
    advertisement_timeout_s: int = Field(ge=1)
    publish: ModelPublishSettings
    codes: ModelHostDownReadingCodes
    emission: ModelEmissionSettings


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHostDownOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostDownOverlay, environ)


def load_example_overlay() -> ModelHostDownOverlay:
    """The packaged synthetic overlay, validated: the drill and the tests run on it."""
    return ModelHostDownOverlay.model_validate(
        yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    )
