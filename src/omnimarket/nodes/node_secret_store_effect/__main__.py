# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_secret_store_effect.

Usage:
    python -m omnimarket.nodes.node_secret_store_effect < request.json

Reads one ModelSecretStoreRequest as JSON on stdin and writes the
ModelSecretStoreResult as JSON on stdout. The result never carries a value
(the field is excluded from serialization), so this entry point can resolve,
list and check, and a create through it is always answered value_missing.
Exit 0 on a success outcome, 3 otherwise.
"""

from __future__ import annotations

import asyncio
import sys

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_request import (
    ModelSecretStoreRequest,
)

_EXIT_NOT_OK = 3


def main() -> int:
    request = ModelSecretStoreRequest.model_validate_json(sys.stdin.read())
    result = asyncio.run(HandlerSecretStore().handle(request))
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.ok else _EXIT_NOT_OK


if __name__ == "__main__":
    raise SystemExit(main())
