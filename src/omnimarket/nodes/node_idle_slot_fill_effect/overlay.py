# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads of the ``idle_slot_fill`` overlay: each key refuses naming itself when nothing supplies it."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
    not_configured_message,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill_overlay import (
    ModelCloudBacklogOverlay,
    ModelIdleSlotFillOverlay,
    ModelReposRoles,
)

OVERLAY_ID = "idle_slot_fill"
REPOS_ID = "repos"
ROLES = ("private_data", "plugin_source", "deploy", "knowledge_base")


def _load_optional[ModelT: BaseModel](
    overlay_id: str,
    model: type[ModelT],
    environ: Mapping[str, str] | None,
) -> tuple[ModelT | None, Path | None]:
    """Resolve the node overlay; a selected malformed source still refuses."""
    where = find_node_overlay(overlay_id, environ)
    if where is None:
        return None, None
    return load_structured_overlay(overlay_id, model, environ), where


def _refuse(overlay_id: str, key: str, where: Path | None) -> NodeOverlayError:
    return NodeOverlayError(
        (f"{where}: " if where else "")
        + not_configured_message(overlay_id, f"{overlay_id}.{key}")
    )


def _field(key: str, environ: Mapping[str, str] | None) -> Any:
    overlay, where = _load_optional(OVERLAY_ID, ModelIdleSlotFillOverlay, environ)
    value = getattr(overlay, key, None) if overlay is not None else None
    if value is None:
        raise _refuse(OVERLAY_ID, key, where)
    return value


def _role_name(role: str, environ: Mapping[str, str] | None) -> str:
    if role not in ROLES:
        raise NodeOverlayError(
            f"{OVERLAY_ID}: role {role!r} is not one of {sorted(ROLES)}"
        )
    repos, where = _load_optional(REPOS_ID, ModelReposRoles, environ)
    name = getattr(repos, role, None) if repos is not None else None
    if not name:
        raise _refuse(REPOS_ID, role, where)
    return str(name)


def mvp_repos(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    return frozenset(_field("mvp_repos", environ))


def process_fix_repos(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    return frozenset(
        _role_name(role, environ) for role in _field("process_fix_roles", environ)
    )


def always_out(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    return frozenset(
        _role_name(role, environ) for role in _field("always_out_roles", environ)
    )


def cloud_backlog(
    environ: Mapping[str, str] | None = None,
) -> ModelCloudBacklogOverlay:
    declaration: ModelCloudBacklogOverlay = _field("cloud_backlog", environ)
    return declaration


def experiment_designs(
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return list(_field("experiment_designs", environ))


def repo_required_kinds(
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Kinds whose backlog items must name a repository; an overlay that lists none requires none."""
    declaration = load_structured_overlay(OVERLAY_ID, ModelIdleSlotFillOverlay, environ)
    kinds = declaration.repo_required_kinds if declaration is not None else None
    return frozenset(kinds or ())


def ledger_read(environ: Mapping[str, str]) -> tuple[str | None, float]:
    """Environment command takes precedence over the node's deployment overlay."""
    declaration, _ = _load_optional(OVERLAY_ID, ModelIdleSlotFillOverlay, environ)
    command = environ.get("ONEX_LEDGER_BUS_READ_COMMAND", "").strip() or (
        declaration.ledger_read_command if declaration else None
    )
    raw = environ.get("ONEX_LEDGER_BUS_TIMEOUT_S", "").strip()
    timeout = (
        float(raw)
        if raw
        else (declaration.ledger_read_timeout_s if declaration else 5.0)
    )
    if not math.isfinite(timeout) or timeout <= 0:
        raise NodeOverlayError("ONEX_LEDGER_BUS_TIMEOUT_S must be finite and positive")
    return command, timeout
