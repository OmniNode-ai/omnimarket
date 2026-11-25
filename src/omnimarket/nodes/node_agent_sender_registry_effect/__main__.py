# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one registry operation: ``python -m omnimarket.nodes.node_agent_sender_registry_effect register --sender N --principal P --key-id K``.

Prints the typed result as JSON (the public key, never the seed) and exits 0 only when the operation was taken.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    EnumAgentSenderRegistryOperation,
    ModelAgentSenderRegistryRequest,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    load_contract,
)
from omnimarket.nodes.node_agent_sender_registry_effect.adapters.adapter_secret_store_registry import (
    SecretStoreRegistryAdapter,
)
from omnimarket.nodes.node_agent_sender_registry_effect.handlers.handler_agent_sender_registry import (
    HandlerAgentSenderRegistry,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-sender-registry")
    parser.add_argument(
        "operation", choices=[op.value for op in EnumAgentSenderRegistryOperation]
    )
    parser.add_argument("--sender")
    parser.add_argument("--principal")
    parser.add_argument("--key-id")
    args = parser.parse_args(argv)
    try:
        request = ModelAgentSenderRegistryRequest(
            operation=EnumAgentSenderRegistryOperation(args.operation),
            sender=args.sender,
            principal=args.principal,
            key_id=args.key_id,
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid_request: {exc}\n")
        return 2
    layout = load_contract()["message_contract"]["settings_store"]
    handler = HandlerAgentSenderRegistry(SecretStoreRegistryAdapter(), layout)
    result = asyncio.run(handler.handle(request))
    sys.stdout.write(result.model_dump_json(exclude_none=True) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
