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
# OMN-17167: when none resolve the gate now exits 2 through a shared preflight that
# distinguishes an UNSET candidate variable from a STALE one and prints the full
# expanded path it probed (rule 8; memory feedback_own_errors_give_full_paths).
#
# OMN-17167 correction (2026-08-30): the first cut of this fix routed every failure
# through a preflight that unconditionally named OMNI_HOME, even though
# OMNIBASE_INFRA_PATH is THIS gate's own, higher-priority override (checked before
# $OMNI_HOME/omnibase_infra/src above) and OMNI_HOME here is only a worktree-friendly
# fallback. Telling an operator who already set (a wrong) OMNIBASE_INFRA_PATH that
# "OMNI_HOME is not set" sends them to fix the wrong variable -- the same
# "blanket-blame" failure mode rule 8 exists to prevent. The preflight below names
# whichever candidate variable was actually set-but-wrong, and only talks about
# OMNI_HOME as a fallback option when OMNIBASE_INFRA_PATH was never set at all.
#
# Once omnimarket's omnibase-infra pin includes this validator, collapse all of the above
# to `uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution`.

set -euo pipefail

# --- OMN-17167 shared preflight ------------------------------------------------
# Deliberately duplicated verbatim in scripts/validation/run_topic_lint.sh (this repo)
# rather than factored into a cross-repo shim library: a hook whose job is to diagnose
# a broken sibling layout must not itself be loaded from that sibling layout.
# omnibase_core/scripts/pre_commit_validate_deterministic_skills.sh keeps its own
# OMNI_HOME-only preflight -- there OMNI_HOME genuinely is the sole variable that
# fallback branch depends on, so no such split applies there.
#
#   $1  human list of the sibling clones THIS hook needs
#   $2  the full OMNIBASE_INFRA_PATH-derived path this hook needed and did not find
#   $3  the full OMNI_HOME-derived path this hook needed and did not find
infra_sibling_preflight_fail() {
  local siblings="$1" infra_path_missing="$2" omni_home_missing="$3"
  if [ -n "${OMNIBASE_INFRA_PATH:-}" ]; then
    echo "OMNIBASE_INFRA_PATH is set to ${OMNIBASE_INFRA_PATH}, but the sibling clone this hook needs (${siblings}) is not there. Missing:" >&2
    echo "  ${infra_path_missing}" >&2
    echo "OMNIBASE_INFRA_PATH must be the omnibase_infra repo root. Example: export OMNIBASE_INFRA_PATH=\$HOME/omninode/omnibase_infra" >&2
  elif [ -n "${OMNI_HOME:-}" ]; then
    echo "OMNIBASE_INFRA_PATH is not set, and OMNI_HOME is set to ${OMNI_HOME}, but the sibling clone this hook needs (${siblings}) is not there either. Missing:" >&2
    echo "  ${omni_home_missing}" >&2
    echo "Set OMNIBASE_INFRA_PATH to the omnibase_infra repo root, or point OMNI_HOME at the directory containing the sibling clones (${siblings}). Example: export OMNIBASE_INFRA_PATH=\$HOME/omninode/omnibase_infra" >&2
  else
    echo "Neither OMNIBASE_INFRA_PATH nor OMNI_HOME is set. Set one of them so this hook can find the sibling clone (${siblings}):" >&2
    echo "  export OMNIBASE_INFRA_PATH=<path to the omnibase_infra repo root>, or" >&2
    echo "  export OMNI_HOME=<directory containing the sibling clones, e.g. \$HOME/omninode>" >&2
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
  echo "  Looked at ./omnibase_infra/src, OMNIBASE_INFRA_PATH=${OMNIBASE_INFRA_PATH:-<unset>}/src," >&2
  echo "  OMNI_HOME=${OMNI_HOME:-<unset>}/omnibase_infra/src, ../omnibase_infra/src, ../../../omnibase_infra/src." >&2
  echo "  Failing closed (OMN-16939)." >&2
  # OMN-17167: the list above prints the candidates with their live values, so a
  # stale variable (set, wrong directory) is no longer byte-indistinguishable from
  # an unset one. The shared preflight below names whichever candidate variable was
  # actually set-but-wrong and the full expanded path.
  infra_sibling_preflight_fail "omnibase_infra" \
    "${OMNIBASE_INFRA_PATH:-}/src/omnibase_infra/validators/subscriber_dispatcher_resolution.py" \
    "${OMNI_HOME:-}/omnibase_infra/src/omnibase_infra/validators/subscriber_dispatcher_resolution.py"
fi

echo "[subscriber-dispatcher-resolution] using validator from ${INFRA_SRC}" >&2
PYTHONPATH="${INFRA_SRC}${PYTHONPATH:+:${PYTHONPATH}}" \
  uv run python -m omnibase_infra.validators.subscriber_dispatcher_resolution \
  "${SCAN_ROOT}" --baseline "${BASELINE}"
