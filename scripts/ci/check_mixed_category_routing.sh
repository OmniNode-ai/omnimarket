#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-14605 / OMN-18013 — mixed-category routing gate, omnimarket side.
#
# handler_wiring derives a handler_routing entry's message category EXACTLY ONCE — an
# explicit entry.message_category, else _derive_message_category(subscribe_topics[0]) —
# and stamps that ONE category on EVERY ModelDispatchRoute the entry registers. An entry
# whose assigned topics span both .cmd. and .evt. therefore leaves the off-category topics
# PERMANENTLY unroutable: MessageDispatchEngine filters on the real per-topic category
# before any handler runs. Every message on them is consumed, DLQ'd and COMMITTED while
# the consumer group reads Stable / LAG 0.
#
# OMN-14605 shipped this gate over src/omnibase_infra ONLY; its own header deferred the
# other repos to a "Lane B" fan-out that never landed. omnimarket — where both OMN-16939
# live victims sat — had no gate at all. This is that fan-out.
#
# The checker lives in omnibase_infra because it imports the REAL production wiring
# helpers (_topics_for_handler_entry, derive_entry_message_category) — the same ones
# _prepare_handler_wiring calls — so the gate cannot drift from the runtime. omnimarket's
# installed omnibase-infra pin does not carry it yet, so an omnibase_infra SOURCE tree is
# resolved instead, exactly as check_subscriber_dispatcher_resolution.sh does. Fail-closed:
# if none resolve, the gate FAILS rather than silently skipping.

set -euo pipefail

# --- OMN-17167 shared preflight (deliberately duplicated verbatim from
# --- scripts/ci/check_subscriber_dispatcher_resolution.sh: a hook whose job is to
# --- diagnose a broken sibling layout must not be loaded from that sibling layout).
infra_sibling_preflight_fail() {
  local siblings="$1" infra_path_missing="$2" omni_home_missing="$3"
  if [ -n "${OMNIBASE_INFRA_PATH:-}" ]; then
    echo "OMNIBASE_INFRA_PATH is set to ${OMNIBASE_INFRA_PATH}, but the sibling clone this hook needs (${siblings}) is not there. Missing:" >&2
    echo "  ${infra_path_missing}" >&2
    echo "OMNIBASE_INFRA_PATH must be the omnibase_infra repo root. Example: export OMNIBASE_INFRA_PATH=\$HOME/omninode/omnibase_infra" >&2
  elif [ -n "${OMNI_HOME:-}" ]; then
    echo "OMNIBASE_INFRA_PATH is not set, and OMNI_HOME is set to ${OMNI_HOME}, but the sibling clone this hook needs (${siblings}) is not there either. Missing:" >&2
    echo "  ${omni_home_missing}" >&2
    echo "Set OMNIBASE_INFRA_PATH to the omnibase_infra repo root, or point OMNI_HOME at the directory containing the sibling clones (${siblings})." >&2
  else
    echo "Neither OMNIBASE_INFRA_PATH nor OMNI_HOME is set. Set one of them so this hook can find the sibling clone (${siblings}):" >&2
    echo "  export OMNIBASE_INFRA_PATH=<path to the omnibase_infra repo root>, or" >&2
    echo "  export OMNI_HOME=<directory containing the sibling clones>" >&2
  fi
  exit 2
}

SCAN_ROOT="${1:-src/omnimarket}"
BASELINE="${2:-config/validation/mixed_category_routing_omnimarket_baseline.yaml}"

INFRA_SRC=""
for candidate in \
  "./omnibase_infra/src" \
  "${OMNIBASE_INFRA_PATH:-}/src" \
  "${OMNI_HOME:-}/omnibase_infra/src" \
  "../omnibase_infra/src" \
  "../../../omnibase_infra/src"; do
  if [[ -d "${candidate}" && -f "${candidate}/omnibase_infra/validators/mixed_category_routing.py" ]]; then
    INFRA_SRC="${candidate}"
    break
  fi
done

if [[ -z "${INFRA_SRC}" ]]; then
  echo "[mixed-category-routing] FAIL: no omnibase_infra source tree carrying" >&2
  echo "  omnibase_infra/validators/mixed_category_routing.py was found." >&2
  echo "  Looked at ./omnibase_infra/src, OMNIBASE_INFRA_PATH=${OMNIBASE_INFRA_PATH:-<unset>}/src," >&2
  echo "  OMNI_HOME=${OMNI_HOME:-<unset>}/omnibase_infra/src, ../omnibase_infra/src, ../../../omnibase_infra/src." >&2
  echo "  Failing closed (OMN-18013)." >&2
  infra_sibling_preflight_fail "omnibase_infra" \
    "${OMNIBASE_INFRA_PATH:-}/src/omnibase_infra/validators/mixed_category_routing.py" \
    "${OMNI_HOME:-}/omnibase_infra/src/omnibase_infra/validators/mixed_category_routing.py"
fi

echo "[mixed-category-routing] using validator from ${INFRA_SRC}" >&2
PYTHONPATH="${INFRA_SRC}${PYTHONPATH:+:${PYTHONPATH}}" \
  uv run python -m omnibase_infra.validators.mixed_category_routing \
  "${SCAN_ROOT}" --baseline "${BASELINE}"
