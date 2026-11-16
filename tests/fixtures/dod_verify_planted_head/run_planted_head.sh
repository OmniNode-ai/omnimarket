#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Runs the node_dod_verify entry point against the planted head in this directory and exits 0 only
# when the node exits 1 with contract_not_synced in its output. Any other outcome exits nonzero:
# the node accepting the planted head, or refusing it for another cause.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

status=0
output="$(
  ONEX_SKILL_OVERLAY_ROOTS="$here/overlay" uv run --frozen python -m omnimarket.nodes.node_dod_verify \
    --ticket-id OMN-1000 \
    --contract-path planted_head.yaml \
    --full-contract-path planted_head.yaml \
    --execution-audience hosted \
    --dry-run 2>&1
)" || status=$?

printf '%s\n' "$output"
if [ "$status" -ne 1 ]; then
  echo "expected the node to exit 1 on the planted head, got $status" >&2
  exit 1
fi
if ! printf '%s\n' "$output" | grep -q contract_not_synced; then
  echo "the node exited 1 but did not name contract_not_synced" >&2
  exit 1
fi
