# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin replay command: dispatch the replay node on its declared bus."""

from pathlib import Path

import click
import yaml
from omnibase_core.cli.cli_run_node import publish_and_poll, run_node_in_process

from omnimarket.handlers.bus_binding.node_broker import resolve_node_broker
from omnimarket.models.error_rule_match import (
    ModelErrorRuleReplayRequest,
    ModelErrorRuleReplayResult,
)

NODE = "node_error_rule_replay_compute"
CONTRACT = Path(__file__).with_name("contract.yaml")


@click.command("error-rule-replay")
@click.option(
    "--input-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="JSON request containing frozen events, facts and rule table.",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=None,
    help="Override the node contract's timeout in seconds.",
)
def replay_command(input_file: Path, timeout: int | None) -> None:
    """Print hits, misses, labelled false matches and sorted NO-MATCH keys."""
    request = ModelErrorRuleReplayRequest.model_validate_json(input_file.read_text())
    contract = yaml.safe_load(CONTRACT.read_text())
    deadline = (
        timeout
        if timeout is not None
        else (contract["descriptor"]["timeout_ms"] + 999) // 1000
    )
    broker = resolve_node_broker(NODE)
    payload = request.model_dump(mode="json")
    response: object
    if broker.kind == "inmemory":
        response = run_node_in_process(NODE, CONTRACT, payload, deadline)
    else:
        envelope = publish_and_poll(
            node_id=NODE,
            payload=payload,
            timeout=deadline,
            bootstrap_servers=broker.bootstrap_servers or "",
            command_topic=contract["runtime_dispatch"]["command_topic"],
            response_topic=contract["terminal_event"],
            client_security=broker.client_security(),
        )
        if envelope is None:
            raise click.ClickException(
                "error-rule replay timed out without a terminal event"
            )
        response = envelope.get("payload", envelope)
    result = ModelErrorRuleReplayResult.model_validate(response)
    click.echo(result.report(), nl=False)
