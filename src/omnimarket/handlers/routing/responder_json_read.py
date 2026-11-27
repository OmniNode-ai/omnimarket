# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of a delegation responder's JSON, through the delegation transport (OMN-26043).

The delegation check probe node reads a declared responder's ``/v1/models`` and ``/slots``. The read lives here, not in the node's
handler directory: the compliance sweep judges a node handler that imports an HTTP client against the transport its contract
declares, and this node's declared transport is the bus. Only a transport failure reads as "the responder did not answer"; any
other error propagates, so a broken environment is never reported as an absent responder.
"""

from __future__ import annotations

import httpx

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    get_provider_json,
)


def read_responder_json(url: str, timeout_s: float) -> object | None:
    """GET a declared responder's JSON; None when it did not answer (transport failure, not a JSON body)."""
    try:
        body: object = get_provider_json(
            url=url, timeout_seconds=timeout_s, trust_env=False
        )
    except (OSError, ValueError, httpx.HTTPError):
        return None
    return body
