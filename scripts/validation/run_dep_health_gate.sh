#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Dependency health gate — pre-commit hook wrapper.
# Delegates to scripts/ci/run_dep_health_sweep.py.
#
# Phased rollout (mirrors GHA dep-health job):
#   Phase 1 (advisory): no baseline file → exit 0, advisory output only
#   Phase 2 (active):   baseline present → --delta-mode blocks new CRITICAL findings
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
  # Phase 2: delta-blocking — fail only when new CRITICAL findings appear vs baseline
  RC=0
  OUTPUT="$(uv run python "$GATE_SCRIPT" \
    --repo-roots "src/" \
    --severity-threshold CRITICAL \
    --baseline-path "$BASELINE_PATH" \
    --delta-mode 2>&1)" || RC=$?
else
  # Phase 1: advisory — gather data without blocking (baseline not yet committed)
  OUTPUT="$(uv run python "$GATE_SCRIPT" \
    --repo-roots "src/" \
    --severity-threshold CRITICAL 2>&1)"
  RC=0
fi

FINDING_COUNT=$(echo "$OUTPUT" | grep -c '"finding_type"' || true)
echo "dep-health-gate: ${FINDING_COUNT} finding(s) detected (threshold: CRITICAL)" >&2

echo "$OUTPUT"
exit $RC
