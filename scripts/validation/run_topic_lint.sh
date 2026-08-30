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
# scripts/ci/check_subscriber_dispatcher_resolution.sh already uses, and failure
# routes through the shared preflight that names OMNI_HOME and the full missing
# path. Doctrine: omni_home CLAUDE.md rule 8 (fail fast on missing env, never a
# silent default) and rule 6 (no absolute paths -- the remediation is an
# ``export`` line, not a machine path).
set -euo pipefail

# --- OMN-17167 shared preflight ------------------------------------------------
# Deliberately duplicated verbatim in scripts/ci/check_subscriber_dispatcher_resolution.sh
# and omnibase_core/scripts/pre_commit_validate_deterministic_skills.sh rather than
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
  echo "  Looked at ./omnibase_infra, \$OMNIBASE_INFRA_PATH, \$OMNI_HOME/omnibase_infra," >&2
  echo "  ../omnibase_infra, ../../../omnibase_infra." >&2
  echo "  Override with: export OMNIBASE_INFRA_PATH=<path to the omnibase_infra repo root>" >&2
  omni_home_preflight_fail "omnibase_infra" "${OMNI_HOME:-}/omnibase_infra/$LINT_REL"
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
