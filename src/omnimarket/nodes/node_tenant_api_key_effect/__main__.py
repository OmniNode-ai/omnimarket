# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send one register or revoke command to the runtime over the bus the node's overlay names.

``register --input <json>`` and ``revoke --input <json>`` validate the command against the node's own request model and
publish it to the topic the contract declares for that operation. The answer arrives on the operation's completed topic,
which the caller reads; this entry never reads the database. The command carries the sha256 digest of a key the caller
holds, never the key. Exit 0 when published; 3 INDETERMINATE on stderr for a bad input or a broker the overlay does not
name. The broker is never taken from an environment variable or a flag.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Final, Never

import yaml
from pydantic import BaseModel

from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_tenant_api_key_effect.handlers.contract_settings import (
    CONTRACT_PATH,
)
from omnimarket.nodes.node_tenant_api_key_effect.models import (
    ModelTenantApiKeyRegisterRequest,
    ModelTenantApiKeyRevokeRequest,
)
from omnimarket.topic_namespace import apply_topic_namespace

NODE_NAME: Final[str] = "node_tenant_api_key_effect"
_OPERATIONS: Final[dict[str, tuple[str, type[BaseModel]]]] = {
    "register": (
        "tenant_api_key_register_requested",
        ModelTenantApiKeyRegisterRequest,
    ),
    "revoke": ("tenant_api_key_revoke_requested", ModelTenantApiKeyRevokeRequest),
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _topic_for(operation: str) -> str:
    """The command topic the contract's handler_routing declares for an operation."""
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    for entry in raw["handler_routing"]["handlers"]:
        if entry["operation"] == operation:
            return str(entry["topic"])
    raise ValueError(f"contract declares no topic for {operation}")


def _client_kwargs(broker: ModelNodeBroker) -> dict[str, str]:
    """The aiokafka SASL options a declared broker sets, without unset keys."""
    kwargs: dict[str, str] = {}
    if broker.sasl_mechanism is not None:
        kwargs["sasl_mechanism"] = broker.sasl_mechanism
    if broker.sasl_username is not None:
        kwargs["sasl_plain_username"] = broker.sasl_username
    if broker.sasl_password is not None:
        kwargs["sasl_plain_password"] = broker.sasl_password.get_secret_value()
    return kwargs


async def publish_command(topic: str, request: BaseModel) -> None:
    from aiokafka import AIOKafkaProducer

    broker = resolve_dialable_broker(NODE_NAME)
    producer = AIOKafkaProducer(
        bootstrap_servers=broker.bootstrap_servers,
        security_protocol=broker.security_protocol or "PLAINTEXT",
        **_client_kwargs(broker),
    )
    await producer.start()
    try:
        await producer.send_and_wait(
            apply_topic_namespace(topic),
            request.model_dump_json().encode("utf-8"),
            key=str(getattr(request, "tenant_id", "")).encode("utf-8"),
        )
    finally:
        await producer.stop()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("operation", choices=sorted(_OPERATIONS))
    parser.add_argument("--input", required=True, help="the command as JSON")
    try:
        args = parser.parse_args(argv)
        operation, model = _OPERATIONS[args.operation]
        request = model.model_validate_json(args.input)
        asyncio.run(publish_command(_topic_for(operation), request))
    except (ValueError, NodeBrokerResolutionError, NodeOverlayError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}\n")
        return 3
    sys.stdout.write(f"published operation={args.operation}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
