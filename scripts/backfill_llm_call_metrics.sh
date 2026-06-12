#!/usr/bin/env bash
# OMN-13001: idempotent backfill — replay the retained
# onex.evt.omniintelligence.llm-call-completed.v1 topic FROM EARLIEST into
# llm_call_metrics. NOT the runtime writer (that is the deployed catalog command
# running handler_llm_cost). Safe to re-run: ON CONFLICT (input_hash) DO NOTHING.
# Requires: POSTGRES_PASSWORD (or OMNIDASH_ANALYTICS_DB_URL), KAFKA_BOOTSTRAP_SERVERS
set -euo pipefail

exec python -m omnimarket.nodes.node_projection_llm_cost.backfill_llm_call_metrics \
    --bootstrap-servers "${KAFKA_BOOTSTRAP_SERVERS:?KAFKA_BOOTSTRAP_SERVERS is required}" \
    --group-id "local.omnimarket.projection-llm-cost.backfill.v1" \
    --until-idle-seconds "${BACKFILL_IDLE_SECONDS:-15}"
