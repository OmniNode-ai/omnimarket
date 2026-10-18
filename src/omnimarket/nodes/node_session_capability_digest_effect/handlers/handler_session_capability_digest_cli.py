# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SessionStart hook entry: one hook JSON document on stdin, the digest text on stdout.

Exits 0 in every case. A session start is never blocked, so an unreadable document, a missing
environment or any failure inside the handler prints nothing and returns 0.
"""

import json
import os
import sys

from omnimarket.nodes.node_session_capability_digest_effect.handlers.handler_session_capability_digest import (
    HandlerSessionCapabilityDigest,
)
from omnimarket.nodes.node_session_capability_digest_effect.models import (
    ModelSessionCapabilityDigestRequest,
)


def _registry_roots(environ: dict[str, str]) -> str:
    entries = [environ.get("ONEX_REGISTRY_ROOTS", ""), environ.get("OMNI_HOME", "")]
    return ":".join(entry for entry in entries if entry)


def main() -> int:
    try:
        document = json.loads(sys.stdin.read() or "{}")
        if not isinstance(document, dict):
            document = {}
        request = ModelSessionCapabilityDigestRequest(
            cwd=str(document.get("cwd") or os.getcwd()),
            source=str(document.get("source") or "startup"),
            registry_roots=_registry_roots(dict(os.environ)),
            state_dir=os.environ.get("ONEX_STATE_DIR", ""),
            uid=os.getuid(),
        )
        result = HandlerSessionCapabilityDigest().handle(request)
        if result.output:
            sys.stdout.write(result.output + "\n")
    except Exception:
        # A hook must never block a session start.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
