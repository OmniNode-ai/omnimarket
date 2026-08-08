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

# =============================================================================
# .200-default host guard for the heavy (full-suite) escalation (OMN-15059)
# =============================================================================
# CLAUDE.md documents that pushes / heavy gate runs default to the `.200`
# execution host, not the local Mac -- but a rule stated only in a doc/prompt
# has zero enforcement force without a call-site mechanism (memory
# feedback_a_rule_is_not_a_mechanism). Evidence this is load-bearing: a
# 2026-07-24 session drove the local Mac to load ~55 / 93% swap running this
# exact full-suite escalation for 115+ minutes before .200 was invoked as a
# rescue instead of having been the execution target from the start. This
# guard fires ONLY on the heavy branch below (full-suite fail-closed
# escalation), never on the fast impacted-subset path -- gating every push
# would get this hook disabled within a week, which is worse than no guard.
#
# This is a ROUTING OPTIMIZATION, not a security control: if host identity
# cannot be determined, FAIL OPEN (let the push proceed on this host) rather
# than lock a developer out of their own repo on an ambiguous read. Do not
# "harden" this into a hard block later -- the failure mode this guard exists
# to prevent is a stalled/contended local machine, not an untrusted push.
PREPUSH_200_HOSTNAME="${PREPUSH_200_HOSTNAME:-stickybeatz-studio}"
guard_full_suite_host() {
  local host lc_host lc_target heavy_what
  # OMN-15408: the caller names WHICH heavyweight run is being guarded, so the
  # refusal names the real cause. Default preserves the OMN-15059 wording for
  # the flag-driven escalation call sites, which pass no argument.
  heavy_what="${1:-heavy fail-closed full-suite escalation}"
  host="$(hostname -s 2>/dev/null || true)"
  if [ -z "$host" ]; then
    log "WARNING: could not determine local hostname -- unable to verify this is the .200 build host; proceeding locally (fail-open: this guard is a routing optimization, not a security gate)."
    return 0
  fi
  lc_host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  lc_target="$(printf '%s' "$PREPUSH_200_HOSTNAME" | tr '[:upper:]' '[:lower:]')"
  if [ "$lc_host" = "$lc_target" ]; then
    return 0
  fi
  if [ -n "${PREPUSH_ALLOW_LOCAL_FULL_SUITE:-}" ]; then
    log "WARNING: DEGRADED-HOST OVERRIDE IN EFFECT (PREPUSH_ALLOW_LOCAL_FULL_SUITE set) -- running ${heavy_what} on '${host}', NOT the designated .200 host ('${PREPUSH_200_HOSTNAME}'). This host has weaker isolation/headroom than .200; treat any evidence from this run as WEAKER than a .200-run gate. See docs/runbooks/200-build-lane-execution-pattern.md."
    return 0
  fi
  die "${heavy_what} triggered on host '${host}', not the designated .200 build host ('${PREPUSH_200_HOSTNAME}')" \
      "push from .200 instead (ssh jonah@stickybeatz-studio.tail75df5e.ts.net, wrap remote commands as zsh -lc \"...\"; see docs/runbooks/200-build-lane-execution-pattern.md for the full pattern), OR set PREPUSH_ALLOW_LOCAL_FULL_SUITE=1 to run the full suite on this host anyway (visible, degraded-evidence override -- do not use as a routine bypass)"
}

# -----------------------------------------------------------------------------
# Heavyweight-SELECTION predicate (OMN-15408)
# -----------------------------------------------------------------------------
# The OMN-15059 guard above was wired to fire on the selector's `is_full_suite`
# FLAG. That is the wrong key: the selector routinely emits
# `is_full_suite=False` with `selected_paths=["tests/"]` -- the entire suite
# arriving as an "impacted subset" -- and those runs sailed straight past the
# guard. Measured on host `omnibook` through a real `git push` on 2026-07-29:
# omnimarket selected `is_full_suite=False paths=[ tests/ ]` and executed
# 13,898 tests in 506s locally with the guard never invoked, while the SAME
# selected work forced via `PREPUSH_FULL_SUITE=1` (`is_full_suite=True
# reason=feature_flag_off paths=[ tests/ ]`) WAS refused. Identical cost,
# opposite outcome, decided by a flag.
#
# SEAM -- what "heavyweight selection" means, exactly: the selection is
# heavyweight when the paths pytest is about to be handed COVER THE ENTIRE
# full-suite target this hook would run on a fail-closed escalation
# (`$FULL_SUITE_TARGET`, defined next to the pytest invocation below so the
# predicate and the actual run can never drift apart). Concretely: some
# selected path is `$FULL_SUITE_TARGET` itself or a directory ANCESTOR of it.
# That is "the selection failed to be a proper narrowing" expressed against the
# selector's own output -- NOT a parallel cost model, no test counting, no
# timing heuristic, nothing this hook does not already parse.
#
# A genuine narrow selection (`tests/unit/scripts/`, a single test module) is
# strictly below the target and stays runnable locally -- the guard must not
# brick every push from a developer's machine, only the ones that are the
# full-suite run wearing a different label.
#
# Keep this function self-contained (target passed in, no globals): it is
# extracted and EXECUTED directly by
# tests/scripts/test_prepush_hook_host_identity_guard.py.
selection_is_whole_suite() {
  local target normalized_target p normalized
  target="$1"
  shift
  [ -n "$target" ] || return 1
  normalized_target="${target%/}/"
  for p in "$@"; do
    [ -n "$p" ] || continue
    normalized="${p%/}/"
    case "$normalized_target" in
      "$normalized"*) return 0 ;;
    esac
  done
  return 1
}

# Early, STATICALLY-known full-suite escalation short-circuits the guard
# before any git ancestry lookup below (OMN-15059 CI fix). `PREPUSH_FULL_SUITE`
# / `ENABLE_SMART_TESTS=off` force the full suite unconditionally, independent
# of which files changed -- so the host identity can and must be checked here,
# before the `git merge-base` call a few lines down. A shallow/single-branch
# checkout (e.g. a CI runner with no `origin/dev` ancestry) can fail that
# merge-base lookup outright, which previously masked this guard's refusal
# message behind a generic "no common ancestor" git error. The DYNAMIC case --
# the governed selector itself escalating to the full suite because of which
# files changed -- still requires the merge-base to compute the diff first,
# and remains guarded at its own call site further down (`guard_full_suite_host`
# inside the `IS_FULL` branch); this early check does not replace that one, it
# only covers the case where the answer is already known without a diff.
case "${ENABLE_SMART_TESTS:-}" in
  false | False | FALSE | 0 | off | OFF) guard_full_suite_host ;;
esac
if [ -n "${PREPUSH_FULL_SUITE:-}" ]; then
  guard_full_suite_host
fi

# =============================================================================
# Ambient-PYTHONPATH sanitization (OMN-14420 / OMN-15019)
# =============================================================================
# omnibase_core / omnibase_infra / omnibase_spi / omnibase_compat are
# THIRD-PARTY pinned dependencies in this repo, resolved from THIS repo's own
# .venv/site-packages via uv.lock -- never from a worktree src/ directory. An
# ambient PYTHONPATH exported by the parent shell/session (e.g. pointing at a
# sibling canonical clone's src/, per the operator's omni_home layout) is
# inserted ahead of site-packages by the interpreter, so `uv run pytest` --
# and the selector module it invokes -- can SILENTLY import a different,
# possibly stale, copy of a runtime dependency than the one this repo's
# uv.lock actually pinned. OMN-15019 was exactly this: a ModuleNotFoundError
# misdiagnosed as an omnibase_infra packaging gap that was actually ambient
# PYTHONPATH shadowing a stale sibling clone.
#
# Sanitize LOUDLY, never silently: log the exact value being stripped (so the
# operator can see this hook changed their environment) and unset it for
# every subprocess this hook spawns. This is a hook-scoped strip only -- it
# does not touch the invoking shell's own PYTHONPATH after the hook exits.
if [ -n "${PYTHONPATH:-}" ]; then
  log "WARNING: ambient PYTHONPATH detected (${PYTHONPATH}) -- stripping it for this pre-push run (OMN-14420: an ambient PYTHONPATH can silently shadow this repo's pinned omnibase_* dependencies with a stale sibling clone). This hook always runs hermetically; see tests/conftest.py's hermetic-import guard for defense-in-depth on direct 'uv run pytest' invocations."
  unset PYTHONPATH
fi

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
#
# OMN-15719: `--ignore=tests/integration` is PATH-based and only covers tests
# physically located under that directory. An `@pytest.mark.integration` test
# living elsewhere (e.g. tests/nodes/test_repository_code_entity_postgres.py)
# is not excluded by the ignore flag and can need a live Postgres this local
# run never provisions (unlike CI's `test`/`integration-guard` jobs, which run
# a postgres:16-alpine service container).
#
# A blanket `-m "not integration"` was considered and REJECTED: a collect-only
# audit (`--ignore=tests/integration -m "integration and not kafka"
# --collect-only`) found 80 integration-marked tests across 24 files outside
# tests/integration/ that this local run previously exercised -- and a
# service-free sample of them (tests/test_handoff_failure_modes.py,
# tests/test_handler_handoff_effect.py,
# tests/test_skill_mapping_input_coverage.py,
# tests/ci/test_cross_repo_contract_deps.py,
# tests/nodes/node_contract_sweep/test_cli_contract_sweep.py) executes clean
# with zero services provisioned. Excluding the whole marker would have
# silently dropped that real, fast, passing local coverage as collateral
# damage from a fix aimed at exactly one Postgres-touching test.
#
# The precise fix is the fixture-level reachability guard in
# tests/conftest.py's `postgres_fixture`: an unreachable Postgres connect now
# `pytest.skip`s instead of raising OSError, so any `@pytest.mark.integration`
# test that goes through the shared fixture degrades gracefully wherever it
# lives, without excluding tests that never touch Postgres at all. Every other
# real-service integration test in this repo already carries the same
# try/except-skip guard on its own connect call (test_rls_tranche2_omn14894.py,
# test_writer_tenant_isolation_omn14898.py,
# test_projection_delegation_tier_distribution_omn13662.py,
# test_delegation_savings_tenant_id_column_omn14058.py,
# test_datasource_postgres.py's `_has_reachable_db` skipif); the sole
# unguarded connect (test_delegation_legacy_schema_reconcile_omn14974.py) sits
# behind an opt-in `OMN14974_POSTGRES_DSN` env var that is unset by default,
# so it self-skips before ever calling `asyncpg.connect`. No blanket marker
# exclusion is needed; the local marker filter matches CI's
# `-m "not kafka"` (see CLAUDE.md's canonical local command) unchanged.
LOCAL_MARKER_FILTER="not kafka"

# SINGLE SOURCE OF TRUTH for "what the heavy run is" (OMN-15408): the
# fail-closed escalation runs exactly this target, and `selection_is_whole_suite`
# measures the impacted-subset selection against this same value. Changing the
# escalation target automatically moves the guard predicate with it.
FULL_SUITE_TARGET="tests/"

if [ "$IS_FULL" = "True" ] || [ "$IS_FULL" = "true" ]; then
  guard_full_suite_host
  log "running FULL suite (fail-closed escalation): uv run pytest ${FULL_SUITE_TARGET} --ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
  # shellcheck disable=SC2086
  uv run pytest "${FULL_SUITE_TARGET}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${PREPUSH_PYTEST_ARGS:-} || RC=$?
elif [ "${#PATHS[@]}" -gt 0 ]; then
  # OMN-15408: guard on the SELECTED WORK, not the is_full_suite flag. A
  # selection that covers the whole full-suite target is the heavy run under
  # another name and must be routed to .200 exactly as the flagged escalation is.
  if selection_is_whole_suite "$FULL_SUITE_TARGET" "${PATHS[@]}"; then
    guard_full_suite_host "whole-suite-equivalent impacted selection (is_full_suite=${IS_FULL}, selected paths [ ${PATHS_STR}] cover the entire '${FULL_SUITE_TARGET}' escalation target)"
  fi
  log "running impacted subset: uv run pytest ${PATHS_STR}--ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
  # shellcheck disable=SC2086
  uv run pytest "${PATHS[@]}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${PREPUSH_PYTEST_ARGS:-} || RC=$?
else
  log "no impacted unit tests mapped for this push (no source/test change contributed a target); nothing to run."
fi

if [ "$RC" -ne 0 ]; then
  log "ERROR: impacted tests failed (pytest exit ${RC})"
  log "REMEDIATION: fix the failing tests, then re-push. Reproduce with: uv run pytest ${PATHS_STR:-tests/} --ignore=tests/integration -m '${LOCAL_MARKER_FILTER}'"
  exit "$RC"
fi

log "impacted tests passed; allowing push."
exit "$RC"
