# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the terminal handler from the node's overlay document; nothing environment-specific is in code."""

from __future__ import annotations

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    HandlerGatewayWorkflowTerminal,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.publisher_dead_letter import (
    KafkaDeadLetterPublisher,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.store_postgres import (
    GatewayTerminalPostgresPool,
    PostgresGatewayTerminalStore,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_overlay import (
    ModelGatewayTerminalOverlay,
)

OVERLAY_ID = "node_gateway_workflow_terminal_effect"


def build_handler_from_overlay() -> HandlerGatewayWorkflowTerminal:
    """Load overlay.yaml and return a handler over the Postgres store and the Kafka dead-letter publisher."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelGatewayTerminalOverlay)
    database = overlay.database
    pool = GatewayTerminalPostgresPool(
        secret_resolver_config_path=database.secret_resolver_config_path,
        dsn_secret=database.dsn_secret,
        max_size=database.pool_max_size,
        query_timeout_seconds=database.query_timeout_seconds,
    )
    return HandlerGatewayWorkflowTerminal(
        PostgresGatewayTerminalStore(pool),
        KafkaDeadLetterPublisher(
            bootstrap_servers_env=overlay.broker_bootstrap_servers_env,
            timeout_seconds=overlay.publish_timeout_seconds,
        ),
    )


__all__: list[str] = ["OVERLAY_ID", "build_handler_from_overlay"]
