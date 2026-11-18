# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection read binding a host's row of the node overlay declares (OMN-23994).

``projection/runner.py`` resolves the read binding from two environment variables that each
name an overlay file. A host that sets neither (the operator Mac) had no binding, so every
projection read of it was refused. This module adds the third source, after those two: the
row for this host in ``<overlay root>/node_projection_read_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``, validated through
:class:`ModelProjectionReadBindingOverlay`.

A host with no row, or no overlay file, has no binding (``None``), so the read node still
refuses ``projection_binding_unconfigured`` and the reader prints UNKNOWN, never a zero. An
overlay file that does not load raises
:class:`~omnimarket.projection.runner.ProjectionReadBindingOverlayError` and never falls back.
This module reads neither binding variable: it asks the runner's resolver first.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.node_overlays.model_projection_read_binding_overlay import (
    ModelProjectionReadBindingOverlay,
)
from omnimarket.projection.runner import (
    ModelProjectionRuntimeBinding,
    ProjectionReadBindingOverlayError,
    projection_read_binding_from_overlay_env,
)

PROJECTION_READ_BINDING_HOST_OVERLAY_ID = "node_projection_read_effect"


def local_host_short_name() -> str:
    """The local host name without any domain suffix."""
    return socket.gethostname().split(".", 1)[0]


def projection_read_binding_from_host_overlay(
    host: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelProjectionRuntimeBinding | None:
    """The binding the node overlay names for ``host`` (default: this host), or ``None``.

    ``None`` when no overlay file is found or no row names the host; never a default
    binding. A found overlay that does not load raises
    :class:`ProjectionReadBindingOverlayError` naming the roots variable, the file and the
    load error's class, never the file's contents.
    """
    env = os.environ if environ is None else environ
    path = find_node_overlay(PROJECTION_READ_BINDING_HOST_OVERLAY_ID, env)
    if path is None:
        return None
    try:
        overlay = load_structured_overlay(
            PROJECTION_READ_BINDING_HOST_OVERLAY_ID,
            ModelProjectionReadBindingOverlay,
            env,
        )
    except NodeOverlayError as exc:
        raise ProjectionReadBindingOverlayError(
            OVERLAY_ROOTS_ENV, str(path), type(exc).__name__
        ) from exc
    wanted = (host if host is not None else local_host_short_name()).casefold()
    for row in overlay.bindings:
        if row.host.casefold() == wanted:
            return ModelProjectionRuntimeBinding(
                kafka_bootstrap_servers=row.kafka_bootstrap_servers,
                kafka_consumer_group=row.kafka_consumer_group,
                database_url_secret_ref=row.database_url_secret_ref,
                source=f"overlay:{path}#{row.host}",
            )
    return None


def resolve_projection_read_binding() -> ModelProjectionRuntimeBinding | None:
    """The read binding in order: the read variable, the runtime variable, the host row."""
    binding = projection_read_binding_from_overlay_env()
    if binding is not None:
        return binding
    return projection_read_binding_from_host_overlay()


__all__ = [
    "PROJECTION_READ_BINDING_HOST_OVERLAY_ID",
    "local_host_short_name",
    "projection_read_binding_from_host_overlay",
    "resolve_projection_read_binding",
]
