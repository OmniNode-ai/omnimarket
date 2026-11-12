"""Per-host projection read binding from the node overlay (OMN-23994).

Lives apart from ``runner`` so the baselined runner module does not grow. The
runner types are imported at call time because ``runner`` imports this module.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.node_overlays.model_projection_read_binding_overlay import (
    ModelProjectionReadBindingOverlay,
)

if TYPE_CHECKING:
    from omnimarket.projection.runner import ModelProjectionRuntimeBinding

# The node overlay id whose overlay.yaml carries the per-host projection read
# binding.
PROJECTION_READ_BINDING_HOST_OVERLAY_ID = "node_projection_read_effect"


def local_host_short_name() -> str:
    """The local host name without any domain suffix."""
    return socket.gethostname().split(".", 1)[0]


def projection_read_binding_from_host_overlay(
    host: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelProjectionRuntimeBinding | None:
    """The projection read binding the node overlay names for ``host``.

    ``host`` defaults to :func:`local_host_short_name`. Returns ``None`` when no
    overlay file is found or no row names the host (never a default binding).
    A found overlay that does not load raises
    ``ProjectionReadBindingOverlayError`` naming the roots variable, the
    file and the load error's class, and never falls back.
    """
    from omnimarket.projection.runner import (
        ModelProjectionRuntimeBinding,
        ProjectionReadBindingOverlayError,
    )

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
