# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and settings of the daily work-ledger parity receipt writer (OMN-23251, from OMN-20536)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelParityReceiptRequest(BaseModel):
    """What the host unit passes: where the ledger and the registry are, and how to reach the projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_root: str = Field(
        min_length=1,
        description="Root whose state directory the parity check writes under.",
    )
    ledger_path: str = Field(
        min_length=1, description="The rolling work ledger to read and append to."
    )
    parity_python: str = Field(
        default="",
        description="Interpreter that runs the parity check (it holds omnimarket and a database driver).",
    )
    verb_command: str = Field(
        default="",
        description="Command prefix of the ledger readers; passed to the parity check as --verb-command so the receipt counts per-verb differences. Empty: the receipt says verb_diff=not-run.",
    )
    dsn_overlay_path: str = Field(
        default="",
        description="Overlay file holding the host variable name, the database and the user.",
    )
    dsn_env_file: str = Field(
        default="",
        description="Env file holding the host and the password, read as text and never sourced.",
    )
    pg_port: str = Field(default="", description="Port of the projection database.")
    host_environment: dict[str, str] = Field(
        default_factory=dict,
        description="Variables a host variable named by the overlay may be read from before the env file.",
    )
    now: str | None = Field(
        default=None, description="UTC timestamp used instead of the clock."
    )
    dry_run: bool = False


class ModelParityReceiptSettings(BaseModel):
    """Every setting of the writer; the overlay file holds them all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_name: str = Field(alias="schema", min_length=1)
    ticket: str = Field(min_length=1)
    umbrella_ticket: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    parity_module: str = Field(min_length=1)
    dsn_env_name: str = Field(min_length=1)
    not_before_hour: int = Field(ge=0, le=23)
    not_before_minute: int = Field(ge=0, le=59)
    parity_timeout_seconds: int = Field(ge=1)
    state_subdir: str = Field(min_length=1)
    archive_subdir: str = Field(min_length=1)
    overlay_host_key: str = Field(min_length=1)
    overlay_database_key: str = Field(min_length=1)
    overlay_user_key: str = Field(min_length=1)
    env_password_key: str = Field(min_length=1)
    appender_command: tuple[str, ...] = Field(min_length=1)
    appender_timeout_seconds: int = Field(ge=1)


class ModelParityReceiptResult(BaseModel):
    """The outcome of one run. ``exit_code`` is the unit's: 0 when nothing is owed or a receipt was appended, 1 on a failure
    or a missing setting. ``status`` is what the runtime reads to classify the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "failure"]
    exit_code: int = Field(ge=0, le=1)
    state: Literal[
        "NOT-YET",
        "PRESENT",
        "DRY-RUN",
        "APPENDED",
        "NOT-CONFIGURED",
        "FAILED",
    ]
    day: str = ""
    reason: Literal["", "no-receipt", "newest-unhealthy", "newest-clean"] = ""
    receipt_row: str = ""
    log_line: str = ""
    error_message: str = ""
