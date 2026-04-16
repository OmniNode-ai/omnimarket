#!/usr/bin/env bash
# OMN-8939 L1 proof repro — runs node_merge_sweep end-to-end on
# RuntimeLocal + EventBusInmemory + ServiceStateDisk with zero OmniNode infra.
#
# Prerequisites: Python 3.12, uv, gh CLI with auth (for the handler's PR fetch).
# See docs/plans/2026-04-16-prove-core-runtime-standalone.md Standalone Proof Boundary.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
STATE_DIR="$(mktemp -d -t onex-l1-proof-XXXX)"
INPUT="${REPO_ROOT}/fixtures/merge_sweep_input_sample.json"

echo "L1 proof — runtime + file state + in-memory bus only"
echo "Node     : node_merge_sweep (packaged contract resolved via onex.nodes entry point)"
echo "Input    : ${INPUT}"
echo "State    : ${STATE_DIR}"
echo "---"

uv run onex node node_merge_sweep --input "${INPUT}" --state-root "${STATE_DIR}" --timeout 30

echo "---"
echo "State file:"
cat "${STATE_DIR}/workflow_result.json"
