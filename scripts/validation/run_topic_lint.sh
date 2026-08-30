#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
#
# Run topic-naming-lint against omnimarket contract YAML files and Python source.
# Mirrors the omnibase_infra pattern. (OMN-8507)
#
# Usage: invoked by pre-commit as a system-language hook, or directly:
#   bash scripts/validation/run_topic_lint.sh
#
# OMN-17167: this script used to resolve omnibase_infra from OMNIBASE_INFRA_PATH
# or the bare relative sibling ``../omnibase_infra`` ONLY, and on failure printed
# nothing but the not-found path. It never read OMNI_HOME, so from a worktree at
# $OMNI_HOME/omni_worktrees/<ticket>/omnimarket the hook was unrunnable and the
# error named no variable the operator could act on (the OMN-14444 mechanism).
# The candidate list below now matches the order
# scripts/ci/check_subscriber_dispatcher_resolution.sh already uses.
#
# OMN-17167 correction (2026-08-30): the first cut of this fix routed every
# failure through a preflight that unconditionally named OMNI_HOME, even though
# OMNIBASE_INFRA_PATH is THIS hook's own, higher-priority override (checked
# before $OMNI_HOME/omnibase_infra below) and OMNI_HOME here is only a
# worktree-friendly fallback added by this same ticket. Telling an operator who
# already set (a wrong) OMNIBASE_INFRA_PATH that "OMNI_HOME is not set" sends
# them to fix the wrong variable -- the same "blanket-blame" failure mode rule 8
# and memory feedback_own_errors_give_full_paths exist to prevent. The preflight
# below names whichever candidate variable was actually set-but-wrong, and only
# talks about OMNI_HOME as a fallback option when OMNIBASE_INFRA_PATH was never
# set at all.
set -euo pipefail

# --- OMN-17167 shared preflight ------------------------------------------------
# Deliberately duplicated verbatim in scripts/ci/check_subscriber_dispatcher_resolution.sh
# (this repo) rather than factored into a cross-repo shim library: a hook whose
# job is to diagnose a broken sibling layout must not itself be loaded from that
# sibling layout. omnibase_core/scripts/pre_commit_validate_deterministic_skills.sh
# keeps its own OMNI_HOME-only preflight -- there OMNI_HOME genuinely is the sole
# variable that fallback branch depends on, so no such split applies there.
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

LINT_REL="scripts/validation/lint_topic_names.py"

# Resolution order, identical to scripts/ci/check_subscriber_dispatcher_resolution.sh:
#   1. ./omnibase_infra          — a CI checkout beside the repo root
#   2. $OMNIBASE_INFRA_PATH      — explicit override
#   3. $OMNI_HOME/omnibase_infra — the local canonical clone (works from a worktree)
#   4. ../omnibase_infra         — sibling clone, e.g. $OMNI_HOME/omnimarket
#   5. ../../../omnibase_infra   — sibling of a worktree at
#                                  $OMNI_HOME/omni_worktrees/<ticket>/omnimarket
# 4 and 5 are RELATIVE, so no absolute path is hardcoded (rule 6).
OMNIBASE_INFRA=""
for candidate in \
  "./omnibase_infra" \
  "${OMNIBASE_INFRA_PATH:-}" \
  "${OMNI_HOME:-}/omnibase_infra" \
  "../omnibase_infra" \
  "../../../omnibase_infra"; do
  if [ -n "$candidate" ] && [ -f "$candidate/$LINT_REL" ]; then
    OMNIBASE_INFRA="$candidate"
    break
  fi
done

if [ -z "$OMNIBASE_INFRA" ]; then
  echo "ERROR: OMN-8507 topic-naming-lint cannot run: $LINT_REL not found." >&2
  echo "  Looked at ./omnibase_infra, OMNIBASE_INFRA_PATH=${OMNIBASE_INFRA_PATH:-<unset>}, OMNI_HOME=${OMNI_HOME:-<unset>}/omnibase_infra," >&2
  echo "  ../omnibase_infra, ../../../omnibase_infra." >&2
  infra_sibling_preflight_fail "omnibase_infra" \
    "${OMNIBASE_INFRA_PATH:-}/$LINT_REL" \
    "${OMNI_HOME:-}/omnibase_infra/$LINT_REL"
fi

LINT="$OMNIBASE_INFRA/$LINT_REL"
BASELINE="$(dirname "${BASH_SOURCE[0]}")/topic_naming_baseline.txt"
RC=0

BASELINE_ARG=""
if [ -f "$BASELINE" ]; then
  BASELINE_ARG="--baseline $BASELINE"
fi

# Resolve a Python interpreter that has PyYAML available.
# Priority: resolved omnibase_infra .venv (has yaml) → $OMNI_HOME venv → uv run
_py_has_yaml() { "$1" -c "import yaml" 2>/dev/null; }
RUN_PYTHON=""
if [ -f "$OMNIBASE_INFRA/.venv/bin/python" ] && _py_has_yaml "$OMNIBASE_INFRA/.venv/bin/python"; then
  RUN_PYTHON="$OMNIBASE_INFRA/.venv/bin/python"
elif [ -n "${OMNI_HOME:-}" ] && [ -f "$OMNI_HOME/omnimarket/.venv/bin/python" ] && _py_has_yaml "$OMNI_HOME/omnimarket/.venv/bin/python"; then
  RUN_PYTHON="$OMNI_HOME/omnimarket/.venv/bin/python"
fi

if [ -n "$RUN_PYTHON" ]; then
  "$RUN_PYTHON" "$LINT" --scan-contracts src/omnimarket/nodes $BASELINE_ARG || RC=$?
  "$RUN_PYTHON" "$LINT" --scan-python src/omnimarket $BASELINE_ARG || RC=$?
else
  uv run python "$LINT" --scan-contracts src/omnimarket/nodes $BASELINE_ARG || RC=$?
  uv run python "$LINT" --scan-python src/omnimarket $BASELINE_ARG || RC=$?
fi

exit "$RC"
