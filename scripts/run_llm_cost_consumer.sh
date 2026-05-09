#!/usr/bin/env bash
# Run the LLM cost projection consumer as a standalone sidecar.
# Requires: POSTGRES_PASSWORD (or OMNIDASH_ANALYTICS_DB_URL), KAFKA_BOOTSTRAP_SERVERS
set -euo pipefail

exec python -m omnimarket.nodes.node_projection_llm_cost.consumer \
    --bootstrap-servers "${KAFKA_BOOTSTRAP_SERVERS:?KAFKA_BOOTSTRAP_SERVERS is required}" \
    --group-id "local.omnimarket.projection-llm-cost.consume.v1"
