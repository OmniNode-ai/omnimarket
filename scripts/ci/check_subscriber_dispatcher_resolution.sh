#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-16939 — subscriber-dispatcher-resolution gate, omnimarket side.
#
# A declared subscribe_topic that resolves to NO registered dispatcher for its own
# (category, message type) is consumed, DLQ'd and COMMITTED on every message. The offset
# advances after DLQ-routing, so LAG can never rise: the consumer group reads Stable /
# MEMBERS 1 / LAG 0 forever while 100% of the traffic is lost. Live-proven on the .201 dev
# lane 2026-08-29 — node_pr_lifecycle_state_reducer took 174 messages in and DLQ'd 174 over
# six hours, and delegation-escalation-triggered was DLQ'ing at 128 per 40 minutes.
#
# The checker itself lives in omnibase_infra
# (omnibase_infra.validators.subscriber_dispatcher_resolution) because it imports the REAL
# production wiring helpers — _topics_for_handler_entry, derive_entry_message_category,
# derive_entry_message_types — the same ones _prepare_handler_wiring calls. A gate that
# re-implements that derivation is free to drift from the runtime, which is exactly how
# this defect class survived three prior gates. There is one implementation, in one repo.
#
# omnimarket's installed omnibase-infra pin does not yet carry the validator, so this
# resolves an omnibase_infra SOURCE tree instead of relying on the release:
#   1. ./omnibase_infra/src          — the CI checkout (see dispatcher-route-coverage.yml)
#   2. $OMNIBASE_INFRA_PATH/src      — explicit override
#   3. $OMNI_HOME/omnibase_infra/src — the local canonical clone
#   4. ../omnibase_infra/src         — sibling clone, e.g. $OMNI_HOME/omnimarket
#   5. ../../../omnibase_infra/src   — sibling of a worktree at
#                                      $OMNI_HOME/omni_worktrees/<ticket>/omnimarket
# 4 and 5 are RELATIVE to the repo root, so no absolute path is hardcoded (rule 6) and the
# hook works in a git worktree, where $OMNI_HOME is not exported into the hook environment.
# Fail-closed: if none resolve, the gate FAILS rather than silently skipping. A gate that
# can no-op is advisory, and advisory checks get ignored (doctrine rule 5).
#
# Once omnimarket's omnibase-infra pin includes this validator, collapse all of the above
# to `uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution`.

set -euo pipefail

SCAN_ROOT="${1:-src/omnimarket}"
BASELINE="${2:-config/validation/subscriber_dispatcher_resolution_baseline.yaml}"

INFRA_SRC=""
for candidate in \
  "./omnibase_infra/src" \
  "${OMNIBASE_INFRA_PATH:-}/src" \
  "${OMNI_HOME:-}/omnibase_infra/src" \
  "../omnibase_infra/src" \
  "../../../omnibase_infra/src"; do
  if [[ -d "${candidate}" && -f "${candidate}/omnibase_infra/validators/subscriber_dispatcher_resolution.py" ]]; then
    INFRA_SRC="${candidate}"
    break
  fi
done

if [[ -z "${INFRA_SRC}" ]]; then
  echo "[subscriber-dispatcher-resolution] FAIL: no omnibase_infra source tree carrying" >&2
  echo "  omnibase_infra/validators/subscriber_dispatcher_resolution.py was found." >&2
  echo "  Looked at ./omnibase_infra/src, \$OMNIBASE_INFRA_PATH/src, \$OMNI_HOME/omnibase_infra/src," >&2
  echo "  ../omnibase_infra/src, ../../../omnibase_infra/src." >&2
  echo "  Set OMNIBASE_INFRA_PATH to an omnibase_infra clone. Failing closed (OMN-16939)." >&2
  exit 1
fi

echo "[subscriber-dispatcher-resolution] using validator from ${INFRA_SRC}" >&2
PYTHONPATH="${INFRA_SRC}${PYTHONPATH:+:${PYTHONPATH}}" \
  uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution \
  "${SCAN_ROOT}" --baseline "${BASELINE}"
