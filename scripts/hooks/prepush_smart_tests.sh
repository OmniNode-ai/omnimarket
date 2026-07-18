#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Pre-push governed impacted-test selector (OMN-13973 / WS7 fan-out, omnimarket).
#
# Runs the FAST LOCAL IMPACTED SUBSET of the unit suite once per `git push`,
# using the SAME governed selector CI uses -- scripts/ci/detect_test_paths.py +
# scripts/ci/test_selection_adjacency.yaml -- NOT a hand-typed `-k`. The selector
# is fail-closed: it escalates to the full unit suite whenever it cannot prove
# narrowing is safe (a shared module -- models/protocols/enums/routing/runtime/
# events -- a test-infra change: tests/conftest.py, tests/fixtures/, pytest.ini,
# pyproject.toml, >=6 changed modules, or the main branch). See root CLAUDE.md
# Rule #4.
#
# SEAM NOTE (per-repo, NOT a paste of the omnibase_core canary #1451):
# omnimarket's scripts/ci/detect_test_paths.py is hardcoded to SRC_PREFIX
# "src/omnimarket/" and its own scripts/ci/test_selection_adjacency.yaml, and its
# CLI accepts ONLY --changed-files-from/--ref-name/--event-name/--adjacency/
# --feature-flag. It does NOT accept --base-ref (the canary's selector does, for a
# pyproject classifier omnimarket does not have) -- passing --base-ref here would
# hard-error "unrecognized arguments". This wrapper therefore matches omnimarket's
# CI seam exactly (.github/workflows/ci.yml: no --base-ref, default adjacency) and
# computes the diff base itself for `git diff` only.
#
# This hook is deliberately NOT byte-parity with an enforced CI context. On CI the
# selector is gated behind ENABLE_SMART_TESTS (off by default during rollout) and
# the enforced merge gate is the FULL suite. This hook is *net-new, fast local
# subset enforcement* -- a fast local mirror that is ADVISORY of the full CI
# suite, run before the push leaves the machine. NET-NEGATIVE-SURFACE: it retires
# the "run the whole unit suite by hand before every push" manual default (root
# CLAUDE.md Rule #4: "Local pre-push: use the governed selector where wired
# (OMN-13973); until that lands, the full local suite remains the fail-closed
# default").
#
# FAIL-LOUD (CLAUDE.md Rule #8): if the diff base, the selector, or its adjacency
# config cannot resolve, this hook HARD-ERRORS with a remediation message and a
# non-zero exit. It never degrades to a green skip -- a gate that cannot run must
# be indistinguishable from a failing gate.
#
# Env overrides (all optional):
#   PREPUSH_BASE_REF     git ref to diff against            (default: origin/dev)
#   PREPUSH_ADJACENCY    adjacency yaml path            (default: selector built-in)
#   PREPUSH_PYTEST_ARGS  extra args appended to the pytest invocation
#   ENABLE_SMART_TESTS   set false/0/off to force the FULL suite (parity with the
#                        CI var name); default here is smart selection ON, because
#                        the whole point of the local hook is the impacted subset.
#   PREPUSH_FULL_SUITE   set non-empty to force the FULL suite.

set -euo pipefail

log() { printf '[prepush-smart-tests] %s\n' "$1" >&2; }
die() {
  log "ERROR: $1"
  log "REMEDIATION: $2"
  exit 1
}

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || die "not inside a git worktree" \
         "run 'git push' from within the omnimarket repository"
cd "$REPO_ROOT"

BASE_REF="${PREPUSH_BASE_REF:-origin/dev}"

# Deterministic diff base: fetch the base ref best-effort so an online push gets
# an up-to-date merge-base, then REQUIRE it to resolve. Offline is tolerated ONLY
# when the ref already exists locally; an entirely unresolvable base HARD-ERRORS
# rather than silently diffing against nothing.
git fetch --quiet origin "${BASE_REF#origin/}" 2>/dev/null || true
if ! git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null; then
  die "base ref '${BASE_REF}' could not be resolved" \
      "fetch it ('git fetch origin ${BASE_REF#origin/}') or set PREPUSH_BASE_REF to a resolvable ref"
fi

BASE_SHA="$(git merge-base "${BASE_REF}" HEAD 2>/dev/null)" \
  || die "no common ancestor between '${BASE_REF}' and HEAD" \
         "rebase your branch onto ${BASE_REF} so a merge-base exists"

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"

CHANGED_FILE="$(mktemp)"
SELECTION_FILE="$(mktemp)"
SELECTION_ERR="$(mktemp)"
trap 'rm -f "$CHANGED_FILE" "$SELECTION_FILE" "$SELECTION_ERR"' EXIT

git diff --name-only "${BASE_SHA}" HEAD > "$CHANGED_FILE"

# Feature-flag: default ON (impacted subset). Honor the CI var name and an
# explicit full-suite override. Neither knob is a silent bypass -- forcing OFF
# runs MORE tests (the whole suite), never fewer.
FLAG="on"
case "${ENABLE_SMART_TESTS:-}" in
  false | False | FALSE | 0 | off | OFF) FLAG="off" ;;
esac
if [ -n "${PREPUSH_FULL_SUITE:-}" ]; then
  FLAG="off"
fi

# DRY: invoke the EXACT module CI runs (scripts.ci.detect_test_paths), matching
# omnimarket's CI seam (NO --base-ref -- omnimarket's selector does not accept it).
# Split on the optional adjacency override to avoid empty-array expansion under
# `set -u` on bash 3.2 (macOS system bash).
run_selector() {
  if [ -n "${PREPUSH_ADJACENCY:-}" ]; then
    uv run python -m scripts.ci.detect_test_paths \
      --changed-files-from "$CHANGED_FILE" \
      --ref-name "$BRANCH" \
      --event-name pull_request \
      --feature-flag "$FLAG" \
      --adjacency "$PREPUSH_ADJACENCY"
  else
    uv run python -m scripts.ci.detect_test_paths \
      --changed-files-from "$CHANGED_FILE" \
      --ref-name "$BRANCH" \
      --event-name pull_request \
      --feature-flag "$FLAG"
  fi
}

if ! run_selector > "$SELECTION_FILE" 2> "$SELECTION_ERR"; then
  log "selector stderr follows:"
  cat "$SELECTION_ERR" >&2 || true
  die "governed test selector failed to resolve a selection" \
      "verify scripts/ci/detect_test_paths.py + scripts/ci/test_selection_adjacency.yaml resolve under 'uv run' in this worktree"
fi

# Parse the selection with stdlib json -- fail loud on any parse error.
read_sel() {
  python3 - "$SELECTION_FILE" "$1" << 'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
val = data[sys.argv[2]]
if isinstance(val, list):
    print("\n".join(val))
else:
    print(val)
PY
}

IS_FULL="$(read_sel is_full_suite)" \
  || die "could not parse selector output (is_full_suite)" \
         "the selector emitted non-JSON; inspect $SELECTION_FILE"
REASON="$(read_sel full_suite_reason 2> /dev/null || true)"

PATHS=()
PATHS_STR=""
while IFS= read -r p; do
  if [ -n "$p" ]; then
    PATHS+=("$p")
    PATHS_STR="${PATHS_STR}${p} "
  fi
done < <(read_sel selected_paths)

log "selection: is_full_suite=${IS_FULL} reason=${REASON:-none} paths=[ ${PATHS_STR}] (feature-flag=${FLAG})"

# Assemble the pytest target set. tests/integration is always ignored -- it needs
# real services and stays a CI-only concern.
RC=0
# Marker parity with CI (.github/workflows/ci.yml + omnimarket CLAUDE.md): the CI
# shards run `-m "not kafka"` (kafka-marked tests need a live broker and are a
# CI-with-services concern). The local escalation must match what CI actually
# enforces, or it diverges into failing on tests CI never runs here (OMN-14746).
if [ "$IS_FULL" = "True" ] || [ "$IS_FULL" = "true" ]; then
  log "running FULL suite (fail-closed escalation): uv run pytest tests/ --ignore=tests/integration -m 'not kafka' ${PREPUSH_PYTEST_ARGS:-}"
  # shellcheck disable=SC2086
  uv run pytest tests/ --ignore=tests/integration -m "not kafka" --tb=short ${PREPUSH_PYTEST_ARGS:-} || RC=$?
elif [ "${#PATHS[@]}" -gt 0 ]; then
  log "running impacted subset: uv run pytest ${PATHS_STR}--ignore=tests/integration -m 'not kafka' ${PREPUSH_PYTEST_ARGS:-}"
  # shellcheck disable=SC2086
  uv run pytest "${PATHS[@]}" --ignore=tests/integration -m "not kafka" --tb=short ${PREPUSH_PYTEST_ARGS:-} || RC=$?
else
  log "no impacted unit tests mapped for this push (no source/test change contributed a target); nothing to run."
fi

if [ "$RC" -ne 0 ]; then
  log "ERROR: impacted tests failed (pytest exit ${RC})"
  log "REMEDIATION: fix the failing tests, then re-push. Reproduce with: uv run pytest ${PATHS_STR:-tests/} --ignore=tests/integration"
  exit "$RC"
fi

log "impacted tests passed; allowing push."
exit "$RC"
