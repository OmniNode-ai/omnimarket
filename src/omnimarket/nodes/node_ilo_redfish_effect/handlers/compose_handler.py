# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one composition of the iLO Redfish handler from its production ports (OMN-21835, OMN-22906).

Both callers, the node's own CLI and the host power node, build the handler here, so the pinned-TLS
transport, the lab-secret-store credentials, the ledger approval source and the file journal are
composed once. This module reads no environment: the caller passes the overlay, its directory and
the ledger path. A ``None`` ledger path is a read-only composition: the approval source reads
nothing, so every write is refused for want of an approval row.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.ilo_redfish.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
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


def compose_ilo_handler(
    overlay: ModelIloOverlay, overlay_dir: Path, ledger_path: Path | None
) -> HandlerIloRedfish:
    def factory(host: ModelIloHost) -> ProtocolRedfishTransport:
        return build_pinned_transport(host, overlay_dir)

    return HandlerIloRedfish(
        overlay=overlay,
        transport_factory=factory,
        credentials=StoreCredentialSource(),
        approvals=LedgerApprovalSource(ledger_path or Path(os.devnull)),
        journal=FileActionJournal(Path(overlay.journal_dir)),
        clock=lambda: datetime.now(UTC),
        sleeper=time.sleep,
        credential_key_template=load_credential_key_template(),
    )
