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
# OMN-17167: when none resolve the gate now exits 2 through the shared OMNI_HOME
# preflight, which distinguishes an UNSET OMNI_HOME from a STALE one and prints the
# full expanded path it probed (rule 8; memory feedback_own_errors_give_full_paths).
#
# Once omnimarket's omnibase-infra pin includes this validator, collapse all of the above
# to `uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution`.

set -euo pipefail

# --- OMN-17167 shared preflight ------------------------------------------------
# Deliberately duplicated verbatim in scripts/validation/run_topic_lint.sh and
# omnibase_core/scripts/pre_commit_validate_deterministic_skills.sh rather than
# factored into a cross-repo shim library: a hook whose job is to diagnose a broken
# sibling layout must not itself be loaded from that sibling layout. Repo-local,
# identical wording.
#
#   $1     human list of the sibling clones THIS hook needs
#   $2...  the full $OMNI_HOME-derived paths this hook needed and did not find
omni_home_preflight_fail() {
  local siblings="$1"
  shift
  if [ -z "${OMNI_HOME:-}" ]; then
    echo "OMNI_HOME is not set. It must be the directory containing the sibling clones (${siblings}). Example: export OMNI_HOME=\$HOME/omninode" >&2
  else
    echo "OMNI_HOME is set to ${OMNI_HOME}, but the sibling clones this hook needs (${siblings}) are not there. Missing:" >&2
    for missing_path in "$@"; do
      echo "  ${missing_path}" >&2
    done
    echo "OMNI_HOME must be the directory containing the sibling clones (${siblings}). Example: export OMNI_HOME=\$HOME/omninode" >&2
  fi
  exit 2
}
# --- end shared preflight ------------------------------------------------------

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
  echo "  Override with: export OMNIBASE_INFRA_PATH=<path to the omnibase_infra repo root>" >&2
  echo "  Failing closed (OMN-16939)." >&2
  # OMN-17167: the list above prints the candidates as LITERAL unexpanded text, so a
  # stale OMNI_HOME (set, wrong directory) produced output byte-indistinguishable from
  # an unset one. The shared preflight names the variable and the full expanded path.
  omni_home_preflight_fail "omnibase_infra" "${OMNI_HOME:-}/omnibase_infra/src/omnibase_infra/validators/subscriber_dispatcher_resolution.py"
fi

echo "[subscriber-dispatcher-resolution] using validator from ${INFRA_SRC}" >&2
PYTHONPATH="${INFRA_SRC}${PYTHONPATH:+:${PYTHONPATH}}" \
  uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution \
  "${SCAN_ROOT}" --baseline "${BASELINE}"
