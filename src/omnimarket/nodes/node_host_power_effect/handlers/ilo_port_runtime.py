# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose the iLO node's handler for the real power backend through the iLO node's one composer (OMN-22906)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.ilo_redfish.model_ilo_overlay import ModelIloOverlay
from omnimarket.nodes.node_host_power_effect.handlers.backend_ilo_redfish import (
    IloPort,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    ModelHostPowerOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.compose_handler import (
    compose_ilo_handler,
)

ILO_NODE = "node_ilo_redfish_effect"


def build_ilo_port(
    power_overlay: ModelHostPowerOverlay, environ: Mapping[str, str] | None = None
) -> IloPort:
    """The iLO node's ``handle``, built from its own overlay and, when set, the ledger the power overlay names.

    Only a write reads the approval ledger, so an unset ledger variable builds a read-only port: a
    status read needs no write-ledger configuration, and a write through it is refused by the iLO
    node itself for want of an approval row.
    """
    env = os.environ if environ is None else environ
    overlay_file = find_node_overlay(ILO_NODE, env)
    if overlay_file is None:
        raise ValueError(f"no overlay file for {ILO_NODE} under the overlay roots")
    ilo_overlay = load_structured_overlay(ILO_NODE, ModelIloOverlay, env)
    ledger = env.get(power_overlay.ilo_ledger_path_env or "", "")
    return compose_ilo_handler(
        ilo_overlay, overlay_file.parent, Path(ledger) if ledger else None
    ).handle
