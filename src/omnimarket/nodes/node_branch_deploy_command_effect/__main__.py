# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Send the deploy agent's signed rebuild command from outside the
lane's runtime (OMN-19998).

Takes ``--input`` (a ModelBranchDeployCommandRequest as JSON, the
payload ``onex run-node --input`` takes), dials the broker the node's
overlay names under ``event_bus``, and hands the request to
``HandlerBranchDeployCommand``, which signs the command and publishes it
to the topic the contract declares for the deploy agent. Exit 0 when
published; 1 when refused or failed (the result JSON on stdout, the
detail on stderr); 3 INDETERMINATE on stderr for a bad input, a broker
the overlay does not name, or an OSError. The broker is never taken
from an environment variable or a flag.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Final, Never

from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import load_overlay
from omnimarket.topic_namespace import apply_topic_namespace

NODE_NAME: Final[str] = "node_branch_deploy_command_effect"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


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


class _KafkaBus:
    """The bus the handler publishes through: one producer per publish, stopped after."""

    def __init__(self, broker: ModelNodeBroker) -> None:
        self._broker = broker

    async def publish(self, topic: str, *, key: bytes, value: bytes) -> None:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self._broker.bootstrap_servers,
            security_protocol=self._broker.security_protocol or "PLAINTEXT",
            **_client_kwargs(self._broker),
        )
        await producer.start()
        try:
            await producer.send_and_wait(apply_topic_namespace(topic), value, key=key)
        finally:
            await producer.stop()


def build_handler(*, environ: Mapping[str, str]) -> HandlerBranchDeployCommand:
    """Keep the bus dial and the handler assembly overridable by hermetic CLI callers."""
    broker = resolve_dialable_broker(NODE_NAME, environ=environ)
    return HandlerBranchDeployCommand(
        event_bus=_KafkaBus(broker), overlay=load_overlay(), environ=environ
    )


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--input", required=True, help="a ModelBranchDeployCommandRequest as JSON"
    )
    try:
        args = parser.parse_args(argv)
        request = ModelBranchDeployCommandRequest.model_validate_json(args.input)
        result = asyncio.run(build_handler(environ=os.environ).handle(request))
    except (ValueError, NodeBrokerResolutionError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    if result.status == "published":
        return 0
    sys.stderr.write(f"{result.detail}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
