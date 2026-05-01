# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared helpers for thin OmniMarket adapter wrappers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelWrapperError(BaseModel):
    """Structured error shape shared by adapter wrappers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    details: dict[str, object] | None = None
    retryable: bool | None = None


class ModelWrapperProgressEvent(BaseModel):
    """Progress event emitted by adapter wrappers while work is in flight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str = Field(default="progress", min_length=1)
    message: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)


class ModelEnvironmentCheck(BaseModel):
    """Single environment dependency check result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1)
    kind: Literal["command", "env"]
    required: bool
    present: bool
    value: str | None = None


class ModelEnvironmentDiagnostics(BaseModel):
    """Environment dependency diagnostics for a wrapper invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    checks: list[ModelEnvironmentCheck]
    missing_required: list[str] = Field(default_factory=list)


def collect_args(
    source: argparse.Namespace | Mapping[str, object] | Iterable[str],
    *,
    parser: argparse.ArgumentParser | None = None,
    include_none: bool = False,
) -> dict[str, object]:
    """Collect wrapper invocation arguments into a plain dictionary."""

    if isinstance(source, argparse.Namespace):
        raw = vars(source)
    elif isinstance(source, Mapping):
        raw = dict(source)
    else:
        if parser is None:
            raise ValueError("parser is required when collecting argv arguments")
        raw = vars(parser.parse_args(list(source)))

    if include_none:
        return dict(raw)
    return {key: value for key, value in raw.items() if value is not None}


def validate_args(
    args: Mapping[str, object],
    *,
    required: Iterable[str] = (),
    allowed: Iterable[str] | None = None,
) -> dict[str, object]:
    """Validate required and allowed wrapper argument keys."""

    collected = dict(args)
    required_set = set(required)
    allowed_set = set(allowed) if allowed is not None else None

    missing = [
        key
        for key in sorted(required_set)
        if key not in collected or collected[key] is None or collected[key] == ""
    ]
    if missing:
        raise ValueError(f"Missing required argument(s): {', '.join(missing)}")

    if allowed_set is not None:
        unknown = sorted(set(collected) - allowed_set)
        if unknown:
            raise ValueError(f"Unknown argument(s): {', '.join(unknown)}")

    return collected


def map_args_to_payload(
    args: Mapping[str, object],
    *,
    field_map: Mapping[str, str] | None = None,
    omit_none: bool = True,
) -> dict[str, object]:
    """Map wrapper arguments into a node payload dictionary."""

    payload: dict[str, object] = {}
    for key, value in args.items():
        if omit_none and value is None:
            continue
        payload_key = (
            field_map[key] if field_map is not None and key in field_map else key
        )
        payload[payload_key] = value
    return payload


def generate_correlation_id() -> UUID:
    """Generate a UUIDv4 correlation identifier for wrapper dispatch."""

    return uuid4()


def format_output(payload: object, *, indent: int = 2) -> str:
    """Format wrapper output as deterministic JSON text when possible."""

    if isinstance(payload, BaseModel):
        return payload.model_dump_json(indent=indent)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, indent=indent, sort_keys=True, default=str)


def handle_timeout(
    *,
    operation: str,
    timeout_ms: int,
    correlation_id: UUID | str | None = None,
) -> ModelWrapperError:
    """Build a structured retryable timeout error."""

    details: dict[str, object] = {
        "operation": operation,
        "timeout_ms": timeout_ms,
    }
    if correlation_id is not None:
        details["correlation_id"] = str(correlation_id)
    return ModelWrapperError(
        code="runtime_timeout",
        message=f"Timed out waiting for {operation}.",
        details=details,
        retryable=True,
    )


def handle_error(
    exc: BaseException,
    *,
    code: str = "wrapper_error",
    retryable: bool | None = None,
    details: Mapping[str, object] | None = None,
) -> ModelWrapperError:
    """Build a structured wrapper error from an exception."""

    message = str(exc).strip() or exc.__class__.__name__
    return ModelWrapperError(
        code=code,
        message=message,
        details=dict(details) if details is not None else None,
        retryable=retryable,
    )


def stream_progress(
    message: str,
    *,
    event: str = "progress",
    payload: Mapping[str, object] | None = None,
    sink: Callable[[str], None] | None = None,
) -> ModelWrapperProgressEvent:
    """Emit one progress event to a sink and return the typed event."""

    progress = ModelWrapperProgressEvent(
        event=event,
        message=message,
        payload=dict(payload) if payload is not None else {},
    )
    target = sink if sink is not None else sys.stderr.write
    target(progress.model_dump_json() + "\n")
    return progress


def check_environment(
    *,
    required_env: Iterable[str] = (),
    optional_env: Iterable[str] = (),
    required_commands: Iterable[str] = (),
    optional_commands: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
    expose_values: bool = False,
) -> ModelEnvironmentDiagnostics:
    """Check local wrapper environment dependencies without mutating state."""

    env = os.environ if environ is None else environ
    custom_path = env.get("PATH") if environ is not None else None
    checks: list[ModelEnvironmentCheck] = []
    missing_required: list[str] = []

    def add_env(name: str, *, required: bool) -> None:
        value = env.get(name)
        present = value is not None and value != ""
        if required and not present:
            missing_required.append(name)
        checks.append(
            ModelEnvironmentCheck(
                name=name,
                kind="env",
                required=required,
                present=present,
                value=value if expose_values and present else None,
            )
        )

    def add_command(name: str, *, required: bool) -> None:
        resolved = shutil.which(name, path=custom_path)
        present = resolved is not None
        if required and not present:
            missing_required.append(name)
        checks.append(
            ModelEnvironmentCheck(
                name=name,
                kind="command",
                required=required,
                present=present,
                value=cast(str, resolved) if expose_values and present else None,
            )
        )

    for name in required_env:
        add_env(name, required=True)
    for name in optional_env:
        add_env(name, required=False)
    for name in required_commands:
        add_command(name, required=True)
    for name in optional_commands:
        add_command(name, required=False)

    return ModelEnvironmentDiagnostics(
        ok=not missing_required,
        checks=checks,
        missing_required=missing_required,
    )
