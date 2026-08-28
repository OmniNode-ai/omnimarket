#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Dependency health gate — pre-commit hook wrapper.
# Delegates to scripts/validation/dep_health_gate_cache.py, which owns the
# content-addressed cache and the machine-wide scan lock (OMN-16816) and shells
# into scripts/ci/run_dep_health_sweep.py on a cache miss.
#
# Phased rollout (mirrors GHA dep-health job), decided by the Python wrapper:
#   Phase 1 (advisory): no baseline file → advisory output, no --delta-mode
#   Phase 2 (active):   baseline present → --delta-mode blocks new CRITICAL findings
#
# OMN-16816: this used to run the full src/ sweep unconditionally on every
# invocation, so N concurrent worktree lanes ran N simultaneous full scans
# (observed: 8-16 copies, load average 53). The cache key is a hash of exactly
# the bytes the sweep reads, so an unchanged tree is a near-instant hit, and the
# miss path is serialized machine-wide so cold lanes queue instead of thrashing.
# A lock that cannot be acquired fails CLOSED — the gate is never skipped.
#
# `python3` (not `uv run`) is deliberate: the cache-hit path must not pay uv
# startup. The wrapper is stdlib-only and calls `uv run` itself on a miss.
#
# Usage: invoked by pre-commit as a system-language hook, or directly:
#   bash scripts/validation/run_dep_health_gate.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
GATE_WRAPPER="$REPO_ROOT/scripts/validation/dep_health_gate_cache.py"

if [ ! -f "$GATE_WRAPPER" ]; then
  echo "ERROR: dep-health gate wrapper not found at $GATE_WRAPPER" >&2
  exit 2
fi

exec python3 "$GATE_WRAPPER" --repo-root "$REPO_ROOT" "$@"
