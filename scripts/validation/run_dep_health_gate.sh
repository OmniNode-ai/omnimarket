#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Dependency health gate — pre-commit hook wrapper.
# Delegates to scripts/ci/run_dep_health_sweep.py.
#
# Phased rollout (mirrors GHA dep-health-gate.yml):
#   - Advisory mode (exit 0): no baseline file present
#   - Delta-blocking mode (exit 1 on new findings): baseline file present
#
# Usage: invoked by pre-commit as a system-language hook, or directly:
#   bash scripts/validation/run_dep_health_gate.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
GATE_SCRIPT="$REPO_ROOT/scripts/ci/run_dep_health_sweep.py"
BASELINE_PATH="$REPO_ROOT/.onex_state/dep_health_baseline.json"

if [ ! -f "$GATE_SCRIPT" ]; then
  echo "ERROR: dep-health gate script not found at $GATE_SCRIPT" >&2
  exit 2
fi

if [ -f "$BASELINE_PATH" ]; then
  # Delta-blocking mode: fail on new findings above threshold vs baseline
  OUTPUT="$(uv run python "$GATE_SCRIPT" \
    --repo-roots "src/" \
    --severity-threshold MAJOR \
    --baseline-path "$BASELINE_PATH" \
    --exit-nonzero-on-findings 2>&1)" || RC=$?
  RC="${RC:-0}"
else
  # Advisory mode: gather data without blocking (baseline not yet committed)
  OUTPUT="$(uv run python "$GATE_SCRIPT" \
    --repo-roots "src/" \
    --severity-threshold MAJOR 2>&1)"
  RC=0
fi

FINDING_COUNT=$(echo "$OUTPUT" | grep -c '"finding_type"' || true)
echo "dep-health-gate: ${FINDING_COUNT} finding(s) detected (threshold: MAJOR)" >&2

echo "$OUTPUT"
exit $RC
