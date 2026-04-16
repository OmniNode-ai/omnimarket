#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-8953 Proof of Life — four-node ONEX pattern end-to-end via onex node.
#
# Invokes the REDUCER node directly via `onex node node_ledger_state_reducer`
# with a synthetic ModelLedgerHashComputed payload. Proves the full substrate
# path end-to-end:
#
#   CLI (cli_node.py) → RuntimeLocal._run_single_handler
#     → HandlerLedgerStateReducer.handle(event)
#     → dict{"state": ..., "intents": ...}
#     → RuntimeLocal._persist_reducer_projection_if_applicable
#     → ProtocolStateStore.put(ModelStateEnvelope)
#     → ServiceStateDisk writes <state_root>/node_ledger_state_reducer/default/state.yaml
#
# Prerequisites: uv, Python 3.12. NO Kafka, NO Postgres, NO Docker, NO .201.
#
# The ORCHESTRATOR + EFFECT + COMPUTE nodes are independently proven via
# unit tests (tests/test_ledger_nodes_unit.py) and semantic golden chain
# (tests/test_golden_chain_ledger.py). This PoL closes the loop on the
# substrate path that OMN-8946 fixed.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
STATE_DIR="$(mktemp -d -t onex-four-node-proof-XXXX)"
INPUT="${REPO_ROOT}/fixtures/ledger_hash_computed.json"

echo "PoL — four-node pattern (REDUCER substrate sink)"
echo "Node     : node_ledger_state_reducer"
echo "Input    : ${INPUT}"
echo "State    : ${STATE_DIR}"
echo "---"

# Negative controls: no OmniNode external infrastructure.
if [[ "$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')" != "0" ]]; then
    echo "WARN: docker has running containers (proof is still valid — nothing we run uses them)"
fi

uv run onex node node_ledger_state_reducer \
    --input "${INPUT}" \
    --state-root "${STATE_DIR}" \
    --timeout 30

echo "---"
STATE_YAML="${STATE_DIR}/node_ledger_state_reducer/default/state.yaml"
echo "State projection:"
cat "${STATE_YAML}"
echo "---"

# Field-level PASS assertion.
python3 - "${STATE_YAML}" <<'EOF'
import sys
import yaml
from pathlib import Path

envelope = yaml.safe_load(Path(sys.argv[1]).read_text())
assert envelope["node_id"] == "node_ledger_state_reducer", envelope
assert envelope["scope_id"] == "default", envelope
assert envelope["data"]["tick_count"] == 1, envelope
assert len(envelope["data"]["last_hash"]) == 64, envelope
assert envelope["data"]["last_line_count"] == 1, envelope
print("PROOF OF LIFE PASS")
EOF
