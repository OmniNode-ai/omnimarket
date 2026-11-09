# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_overlay_drift_compute: every setting of the join check, none in code.

Found at ``<root>/node_overlay_drift_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it, and a missing overlay
refuses the run, so no consumer, path, unit or park list is ever spelled in code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_overlay_drift_compute"


class ModelValueSource(BaseModel):
    """Where one value lives and how to read it.

    ``kind`` ``yaml_key`` reads the dotted key path ``locator`` of a YAML file. ``kind`` ``regex`` reads
    the group named ``value`` of the first match of ``locator`` in a text file (a shell default such as
    ``${NAME:-5}`` is read with a pattern that captures the 5). ``unit`` names the entry of the
    overlay's ``units`` a bare number is in; ``null`` means the value is not a quantity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    kind: Literal["yaml_key", "regex"]
    locator: str = Field(min_length=1)
    unit: str | None

    @model_validator(mode="after")
    def _locator_shape(self) -> ModelValueSource:
        if self.kind == "regex":
            try:
                pattern = re.compile(self.locator, re.MULTILINE)
            except re.error as exc:
                raise ValueError(f"locator is not a regular expression: {exc}") from exc
            if "value" not in pattern.groupindex:
                raise ValueError("a regex locator needs a group named value")
        return self


class ModelConsumer(BaseModel):
    """One consumer of one overlay key: the overlay's value, the consumer's value, an optional env override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    overlay_value: ModelValueSource
    consumer_value: ModelValueSource
    #: A variable that, when set and non-empty in the run's environment, is the consumer's effective value.
    #: The key is required; ``null`` declares the consumer has no override.
    env_override: str | None


class ModelParkListScan(BaseModel):
    """A file outside every overlay and the pattern that would define a park list in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    path: str = Field(min_length=1)
    pattern: str = Field(min_length=1)

    @model_validator(mode="after")
    def _pattern_compiles(self) -> ModelParkListScan:
        try:
            re.compile(self.pattern, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"pattern is not a regular expression: {exc}") from exc
        return self


class ModelJoinPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh: the daily cadence plus its run bound.
    publish_max_age_seconds: int = Field(ge=1)
    #: The most findings a row names; the counts always carry the full numbers.
    max_named: int = Field(ge=1)
    #: The most characters of one value a row shows.
    value_chars: int = Field(ge=1)


class ModelOverlayDriftComputeOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Unit name to its factor into one common base unit (for example MiB = 1048576 with GiB = 1073741824).
    units: dict[str, int]
    #: What the common base unit is called in a row.
    base_unit: str = Field(min_length=1)
    consumers: tuple[ModelConsumer, ...] = Field(min_length=1)
    #: Files outside any overlay that must not hold a park list; the key is required and may be empty.
    park_list_scans: tuple[ModelParkListScan, ...]
    ledger_append: ModelLedgerAppendRoute
    publish: ModelJoinPublish
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelOverlayDriftComputeOverlay:
        if any(factor < 1 for factor in self.units.values()):
            raise ValueError("a unit factor is below 1")
        names = [consumer.name for consumer in self.consumers]
        if len(names) != len(set(names)):
            raise ValueError("a consumer is declared twice")
        scans = [scan.name for scan in self.park_list_scans]
        if len(scans) != len(set(scans)):
            raise ValueError("a park list scan is declared twice")
        for consumer in self.consumers:
            for source in (consumer.overlay_value, consumer.consumer_value):
                if source.unit is not None and source.unit not in self.units:
                    raise ValueError(
                        f"consumer {consumer.name} names undeclared unit {source.unit}"
                    )
        return self

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on: units, consumers, scans and the publish rule."""
        text = json.dumps(
            {
                "units": self.units,
                "base_unit": self.base_unit,
                "consumers": [c.model_dump(mode="json") for c in self.consumers],
                "park_list_scans": [
                    s.model_dump(mode="json") for s in self.park_list_scans
                ],
                "publish": self.publish.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelOverlayDriftComputeOverlay:
    return load_structured_overlay(NODE_NAME, ModelOverlayDriftComputeOverlay, environ)
