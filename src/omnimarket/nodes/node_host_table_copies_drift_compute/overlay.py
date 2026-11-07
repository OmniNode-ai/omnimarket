# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_table_copies_drift_compute: every setting of the read, none in code.

Found at ``<root>/node_host_table_copies_drift_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it, and a missing overlay
refuses the run, so no copy path is ever spelled in code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_host_table_copies_drift_compute"


class ModelCopySettings(BaseModel):
    """One declared copy of the host table: its name in the reading and where its file is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    path: str = Field(min_length=1)


class ModelTableSettings(BaseModel):
    """How a copy is laid out: the key holding its hosts and the key naming each host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts_key: str = Field(min_length=1)
    name_key: str = Field(min_length=1)


class ModelDriftPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh: the daily cadence plus its run bound.
    publish_max_age_seconds: int = Field(ge=1)
    #: The most differences a row names; the counts always carry the full numbers.
    max_named: int = Field(ge=1)
    #: The most characters of one value a row shows.
    value_chars: int = Field(ge=1)


class ModelIntentionalDifference(BaseModel):
    """A difference the owners declare on purpose: one field of one host, absent from named copies only.

    It is declared exactly: the field must be absent from exactly the copies named in ``absent_in``
    and equal in every other copy that holds it. A different value, a different set of copies, or a
    declaration that no longer matches any difference is not covered and reads as drift.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    field: str = Field(min_length=1)
    absent_in: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelHostTableCopiesDriftOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    copies: tuple[ModelCopySettings, ...] = Field(min_length=2)
    table: ModelTableSettings
    absent_token: str = Field(min_length=1)
    host_row_field: str = Field(min_length=1)
    ledger_append: ModelLedgerAppendRoute
    publish: ModelDriftPublish
    injected_fault: str = Field(min_length=1)
    #: Differences declared intentional; the key is required and may be an empty list.
    intentional_differences: tuple[ModelIntentionalDifference, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelHostTableCopiesDriftOverlay:
        names = [copy.name for copy in self.copies]
        if len(names) != len(set(names)):
            raise ValueError("a copy is declared twice")
        declared: set[tuple[str, str]] = set()
        for item in self.intentional_differences:
            if (item.host, item.field) in declared:
                raise ValueError(
                    f"intentional difference {item.host}.{item.field} is declared twice"
                )
            declared.add((item.host, item.field))
            unknown = sorted(set(item.absent_in) - set(names))
            if unknown:
                raise ValueError(
                    f"intentional difference {item.host}.{item.field} names undeclared copies {unknown}"
                )
            if set(item.absent_in) == set(names):
                raise ValueError(
                    f"intentional difference {item.host}.{item.field} is absent from every copy"
                )
        return self

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on: the copy names, the layout and the publish rule."""
        text = json.dumps(
            {
                "copies": [copy.name for copy in self.copies],
                "table": self.table.model_dump(mode="json"),
                "absent_token": self.absent_token,
                "host_row_field": self.host_row_field,
                "publish": self.publish.model_dump(mode="json"),
                "intentional_differences": [
                    item.model_dump(mode="json")
                    for item in self.intentional_differences
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostTableCopiesDriftOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostTableCopiesDriftOverlay, environ)
