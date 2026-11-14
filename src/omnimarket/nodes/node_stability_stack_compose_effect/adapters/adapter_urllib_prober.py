# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AdapterUrllibProber — real ``urllib`` implementation of the health prober.

One plain GET per call for the compose effect node (OMN-23968): the URL comes
from the handler (built from the stack's ports and the health read), the
timeout from the compose overlay. The HTTP status is returned as ``int`` —
including error statuses, so an expected 503 is comparable — and ``0`` when
there is no answer at all (connection refused, DNS failure, timeout, malformed
URL). Nothing is retried here; the handler decides what a status means.

Related:
    - OMN-23968: stability stack compose effect node.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Final

__all__ = ["AdapterUrllibProber"]

_READ_LIMIT_BYTES: Final = 65536


class AdapterUrllibProber:
    """GETs ``url`` and returns its HTTP status, ``0`` when there is no answer.

    Implements :class:`~omnimarket.nodes.node_stability_stack_compose_effect.protocols.protocol_stack_health_prober.ProtocolStackHealthProber`
    structurally (``status(url, timeout_seconds) -> int``).
    """

    def status(self, url: str, timeout_seconds: float) -> int:
        """Return the response status of one GET of ``url``, or ``0`` on no answer."""
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"),
                timeout=timeout_seconds,
            ) as response:
                status = int(response.status)
                response.read(_READ_LIMIT_BYTES)
                return status
        except urllib.error.HTTPError as http_error:
            # An answered request with an error status is still an answer.
            return int(http_error.code)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return 0
