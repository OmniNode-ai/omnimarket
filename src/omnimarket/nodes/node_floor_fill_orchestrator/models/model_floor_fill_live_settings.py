# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live overlay keys (``floor_fill_live.*``). No default: a missing key refuses naming it.

The six seam keys (``SEAM_KEYS``) are the one exception while the overlays repository catches up: the overlays main
document carries none of them yet, and the overlay main-load check loads that document through this model. Each is
optional here and the seam it feeds answers ``UNKNOWN`` naming the absent key until the overlay supplies it; the keys
become required again once the overlays main document carries them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omnimarket.models.stop_gate import EnumStopMode, ModelStopPolicy
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_config import (
    FloorFillConfigError,
)

LIVE_OVERLAY_SECTION = "floor_fill_live"
LINE_ID_PLACEHOLDER = "{line_id}"

_PREFIX = f"{LIVE_OVERLAY_SECTION}."

#: The keys the pool, tickets, brake and stop seams read; absent keys leave their seam unread, never unbound.
SEAM_KEYS: tuple[str, ...] = (
    "pool_snapshot_path",
    "pool_record_suffix",
    "pool_snapshot_max_age_seconds",
    "pool_engine",
    "mint_timeout_seconds",
    "andon_state_dir",
)


class ModelFloorFillLiveSettings(BaseModel):
    """The live bindings' overlay keys. Every one is required except the six ``SEAM_KEYS`` and the three policy keys.

    The policy keys (``brake_min_admitting_hosts``, ``stop_modes``, ``stop_enforce_citations``) are choices, not deployment
    facts: the brake bound defaults to one host, and an overlay that supplies no ``stop_modes`` leaves the stop policy to the
    ``andon_stop`` overlay of the stop reducer.

    ``store_dsn_env`` is the NAME of the environment variable that holds the store DSN, never the DSN
    itself; the binding site reads it, so the secret never lands in the overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_dsn_env: str = Field(min_length=1)
    store_schema_name: str = Field(min_length=1)
    store_table_name: str = Field(min_length=1)
    store_connect_timeout_seconds: int = Field(gt=0)
    floor_fetch_timeout_seconds: float = Field(gt=0)
    plan_file_pattern: str = Field(min_length=1)
    ledger_path: str = Field(min_length=1)
    process_output_dir: str = Field(min_length=1)
    # The pool seam: the lane host pool snapshot file the snapshot writer keeps, the suffix of the read-at record
    # beside it, how old that record may be, and the engine the pool is read for.
    pool_snapshot_path: str | None = Field(default=None, min_length=1)
    pool_record_suffix: str | None = Field(default=None, min_length=1)
    pool_snapshot_max_age_seconds: int | None = Field(default=None, gt=0)
    pool_engine: str | None = Field(default=None, min_length=1)
    # The tickets seam: the deadline of each call of the ticket store (search, create, read).
    mint_timeout_seconds: float | None = Field(default=None, gt=0)
    # The brake and stop-signal seams: the directory of the andon stop queue (the exception queue's state directory).
    andon_state_dir: str | None = Field(default=None, min_length=1)
    # The brake over the pool snapshot: it is engaged (landing work only) when fewer hosts than this admit a further lane.
    brake_min_admitting_hosts: int = Field(default=1, ge=1)
    # The ``andon_stop`` policy the stop signal asks the stop gate with (class -> observe|enforce, and the citation that moved
    # an enforced class). Absent, the policy is the ``andon_stop`` overlay of the stop reducer.
    stop_modes: dict[str, EnumStopMode] | None = None
    stop_enforce_citations: dict[str, str] = Field(default_factory=dict)
    # The env file the secret store writes the DSN variable into; optional, because the unit's own
    # EnvironmentFile may already carry the variable.
    store_dsn_env_file: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        if isinstance(data, dict):
            missing = [
                name
                for name, field in cls.model_fields.items()
                if field.is_required() and data.get(name) is None
            ]
            if missing:
                keys = tuple(f"{_PREFIX}{name}" for name in missing)
                raise ValueError("no overlay supplies " + ", ".join(keys))
        return data

    @model_validator(mode="after")
    def _enforced_classes_carry_their_citation(self) -> Self:
        if self.stop_modes is None:
            if self.stop_enforce_citations:
                raise ValueError(
                    f"{_PREFIX}stop_enforce_citations is set and {_PREFIX}stop_modes is not"
                )
            return self
        for class_id, mode in self.stop_modes.items():
            if (
                mode is EnumStopMode.ENFORCE
                and not self.stop_enforce_citations.get(class_id, "").strip()
            ):
                raise ValueError(
                    f"{_PREFIX}stop_enforce_citations.{class_id} is missing: a class in enforce mode needs "
                    "the citation that moved it"
                )
        return self

    def stop_policy(self) -> ModelStopPolicy | None:
        """The stop policy the settings supply, or ``None`` when the ``andon_stop`` overlay is to supply it."""
        if self.stop_modes is None:
            return None
        return ModelStopPolicy(
            modes=dict(self.stop_modes),
            enforce_citations=dict(self.stop_enforce_citations),
        )

    @model_validator(mode="after")
    def _plan_pattern_names_the_line(self) -> Self:
        if LINE_ID_PLACEHOLDER not in self.plan_file_pattern:
            raise ValueError(
                f"{_PREFIX}plan_file_pattern carries no {LINE_ID_PLACEHOLDER} element"
            )
        return self


def load_floor_fill_live_settings(
    section: Mapping[str, Any],
) -> ModelFloorFillLiveSettings:
    """Build the live settings from the overlay's ``floor_fill_live`` mapping, refusing naming the keys."""
    try:
        return ModelFloorFillLiveSettings.model_validate(dict(section))
    except ValidationError as error:
        messages = [str(item["msg"]) for item in error.errors()]
        text = "; ".join(messages)
        located = [
            f"{_PREFIX}{'.'.join(str(part) for part in item['loc'])}"
            for item in error.errors()
            if item["loc"]
        ]
        keys = tuple(
            dict.fromkeys(
                [
                    *located,
                    *(
                        token.rstrip(",;)(")
                        for token in text.replace(",", " ").split()
                        if token.startswith(_PREFIX)
                    ),
                ]
            )
        )
        raise FloorFillConfigError(text, keys) from error


__all__: list[str] = [
    "LINE_ID_PLACEHOLDER",
    "LIVE_OVERLAY_SECTION",
    "SEAM_KEYS",
    "ModelFloorFillLiveSettings",
    "load_floor_fill_live_settings",
]
