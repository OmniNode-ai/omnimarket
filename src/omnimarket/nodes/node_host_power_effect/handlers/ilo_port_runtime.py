# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose the iLO node's handler for the real power backend, the way its own CLI does (OMN-22906)."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.ilo_redfish.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_host_power_effect.handlers.backend_ilo_redfish import (
    IloPort,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    ModelHostPowerOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.credential_store import (
    StoreCredentialSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    HandlerIloRedfish,
    load_credential_key_template,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    FileActionJournal,
    LedgerApprovalSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.transport_pinned_https import (
    build_pinned_transport,
)

ILO_NODE = "node_ilo_redfish_effect"


def build_ilo_port(
    power_overlay: ModelHostPowerOverlay, environ: Mapping[str, str] | None = None
) -> IloPort:
    """The iLO node's ``handle``, built from its own overlay and the ledger the power overlay names."""
    env = os.environ if environ is None else environ
    overlay_file = find_node_overlay(ILO_NODE, env)
    if overlay_file is None:
        raise ValueError(f"no overlay file for {ILO_NODE} under the overlay roots")
    ilo_overlay = load_structured_overlay(ILO_NODE, ModelIloOverlay, env)
    ledger_env = power_overlay.ilo_ledger_path_env
    ledger = env.get(ledger_env or "", "")
    if not ledger:
        raise ValueError("the ledger path variable the overlay names is not set")
    overlay_dir = overlay_file.parent

    def factory(host: ModelIloHost) -> ProtocolRedfishTransport:
        return build_pinned_transport(host, overlay_dir)

    return HandlerIloRedfish(
        overlay=ilo_overlay,
        transport_factory=factory,
        credentials=StoreCredentialSource(),
        approvals=LedgerApprovalSource(Path(ledger)),
        journal=FileActionJournal(Path(ilo_overlay.journal_dir)),
        clock=lambda: datetime.now(UTC),
        sleeper=time.sleep,
        credential_key_template=load_credential_key_template(),
    ).handle
