# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live overlay keys (``floor_fill_live.*``). No default: a missing key refuses naming it."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_config import (
    FloorFillConfigError,
)

LIVE_OVERLAY_SECTION = "floor_fill_live"
LINE_ID_PLACEHOLDER = "{line_id}"

_PREFIX = f"{LIVE_OVERLAY_SECTION}."
STORE_DSN_ENV_FILE_KEY = f"{_PREFIX}store_dsn_env_file"

# An absolute path of characters a systemd unit line carries unquoted and unexpanded: no whitespace, control
# character, quote, backslash or ``%``. Matched with ``fullmatch`` so a trailing newline is not accepted.
_UNIT_LINE_PATH = re.compile(r"/[A-Za-z0-9_@+=:,./~-]+")


def unit_line_path_refusal(path: str) -> str | None:
    """The refusal text for an environment file path a unit line cannot carry unquoted, or ``None`` when it can."""
    if _UNIT_LINE_PATH.fullmatch(path) is None:
        return f"{STORE_DSN_ENV_FILE_KEY} {path!r} is not an absolute path a unit line can carry unquoted"
    return None


class ModelFloorFillLiveSettings(BaseModel):
    """The live bindings' overlay keys. Every one is required.

    ``store_dsn_env`` is the NAME of the environment variable that holds the store DSN, never the DSN
    itself; the binding site reads it, so the secret never lands in the overlay.

    ``store_dsn_env_file`` is optional: the absolute path of an environment file (``<store_dsn_env>=<value>`` lines)
    that the rendered tick unit reads as ``EnvironmentFile=-<path>``, so a unit the schedule install writes can carry
    the variable without the value ever entering the overlay, the unit text or the install's argv. Absent, the unit
    carries no such line and the variable must come from the unit's own environment.
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
    store_dsn_env_file: str | None = None

    @field_validator("store_dsn_env_file")
    @classmethod
    def _env_file_is_a_unit_line_path(cls, value: str | None) -> str | None:
        """Install and the tick runtime both load the settings here, so both refuse a path the unit cannot carry."""
        if value is not None:
            refusal = unit_line_path_refusal(value)
            if refusal is not None:
                raise ValueError(refusal)
        return value

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
    "STORE_DSN_ENV_FILE_KEY",
    "ModelFloorFillLiveSettings",
    "load_floor_fill_live_settings",
    "unit_line_path_refusal",
]
