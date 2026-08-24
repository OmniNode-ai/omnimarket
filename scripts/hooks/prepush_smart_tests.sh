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
# Recursion guard (OMN-16489, F-01)
# =============================================================================
# This hook spawns pytest, and the spawned suite contains tests that exec THIS
# script again (tests/scripts/test_prepush_hook_host_identity_guard.py and
# siblings). OMN-16425 proved one leaked override var turns that re-entry into
# a recursive full-suite launcher (~9h03m lost across 5 failed ~1h45m runs;
# friction report F-01) — and its fix covered the test sites, not the hook.
# The env scrub at the pytest invocations below closes the override-
# inheritance vector; this sentinel closes the re-entry class itself: a nested
# invocation refuses fail-closed before the selector resolves or any pytest
# spawns. The sentinel deliberately survives the override scrub — children
# must inherit it for this guard to hold. A test that intends to exercise this
# script's FIRST-entry behavior must strip ONEX_PREPUSH_HOOK_ACTIVE from the
# subprocess env it constructs.
if [ -n "${ONEX_PREPUSH_HOOK_ACTIVE:-}" ]; then
  die "nested invocation refused: this hook is already active in an ancestor process (ONEX_PREPUSH_HOOK_ACTIVE=${ONEX_PREPUSH_HOOK_ACTIVE}, this pid $$)" \
      "a pre-push hook run must never be spawned from inside another pre-push hook run (OMN-16425 recursion class). If a test means to exercise first-entry behavior, construct the subprocess env explicitly and strip ONEX_PREPUSH_HOOK_ACTIVE"
fi
export ONEX_PREPUSH_HOOK_ACTIVE="$$"

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
# An UNRESOLVABLE hostname fails CLOSED (OMN-16489 defect 3, redesign plan
# 2026-08-24 §4 S0 item 3 / C2 — supersedes the earlier fail-open note here).
# Heavy runs are routed BY host identity; a host that cannot be identified
# cannot be routed, and proceeding on that silence is the same assumed-
# headroom failure class as the load-probe incidents below. The refusal is
# cheap (<1s, before any pytest) and names its remediation, consistent with
# this hook's fail-loud doctrine: a gate that cannot run must be
# indistinguishable from a failing gate.
PREPUSH_200_HOSTNAME="${PREPUSH_200_HOSTNAME:-stickybeatz-studio}"

# =============================================================================
# Live-load host selection (OMN-16295)
# =============================================================================
# Extends the host-IDENTITY guard below with a CAPACITY dimension: `.200`
# being the right host by IDENTITY does not mean it has headroom. Measured
# 2026-08-20: `.200` load average 32-34 against 24 cores (and, live during
# this same investigation, 56/24 -- 2.3x oversubscribed) driving an 89-93
# minute full-suite run with orphaned pytest processes left behind. Same
# failure class as the 2026-07-24 incident described below, recurring under
# concurrent-session load. OMN-16295 adds a second execution target -- a
# hard-capped gate-runner container on `.201`
# (docker/docker-compose.gate-runner.yml in omnibase_infra), selected ONLY
# when `.200` is over threshold, never the default.
#
# FAIL-CLOSED, unlike the host-IDENTITY guard's fail-open posture below --
# deliberately different, not inconsistent. An unresolvable HOSTNAME is
# ambiguous evidence about WHERE we are (fail open: don't lock a developer out
# of their own repo on a shaky read). An unresolvable LOAD reading is a
# failure to prove EITHER candidate host has capacity, and proceeding anyway
# on that silence is exactly how the 2026-07-24 / 2026-08-20 incidents
# happened -- assumed headroom that was not there. "Neither host reachable"
# refuses; it does not skip the check.
PREPUSH_201_GATE_RUNNER_HOSTNAME="${PREPUSH_201_GATE_RUNNER_HOSTNAME:-gate-runner-201}"
PREPUSH_200_SSH_TARGET="${PREPUSH_200_SSH_TARGET:-jonah@stickybeatz-studio.tail75df5e.ts.net}"  # onex-allow-internal-ip OMN-16295 reason="pre-push guard needs the real host target to probe live load"
PREPUSH_201_SSH_TARGET="${PREPUSH_201_SSH_TARGET:-jonah@192.168.86.201}"  # onex-allow-internal-ip OMN-16295 reason="pre-push guard needs the real host target to probe live load" # fallback-ok: real .201 host target, not a dev/local placeholder
# load1/cores at or under this ratio counts as "fit". 1.0 == "not
# oversubscribed" (a standard load-average heuristic); correctly reads the
# observed-fit `.201` snapshot (~0.4x, 2026-08-20) as fit and both observed
# `.200` snapshots above (1.33x and 2.3x) as over threshold.
PREPUSH_LOAD_THRESHOLD="${PREPUSH_LOAD_THRESHOLD:-1.0}"

# Cross-platform (Linux `.201` / macOS `.200`) load probe: os.getloadavg()[0]
# and os.cpu_count() are both POSIX-portable via the stdlib, which sidesteps
# needing separate /proc/loadavg-vs-sysctl branches (and their escaping) in
# both the local AND the remote-via-ssh cases below. No quote characters
# appear in this snippet -- it is embedded inside a single-quoted remote
# command string in the ssh branch, so it must stay that way.
_PREPUSH_LOAD_PROBE_PY='import os,sys
n=os.cpu_count() or 0
sys.exit(1) if n<=0 else print(os.getloadavg()[0], n)'

# Prefer GNU coreutils timeout(1); fall back to gtimeout(1) (Homebrew name on
# macOS); fall back to no wrapper at all (ssh -o ConnectTimeout already bounds
# the connection phase, and the remote command is a single fast python3 -c).
_prepush_timeout_cmd() {
  if command -v timeout > /dev/null 2>&1; then
    printf 'timeout'
  elif command -v gtimeout > /dev/null 2>&1; then
    printf 'gtimeout'
  fi
}

# host_load_ratio TARGET -- prints "<load1> <nproc> <ratio>" and returns 0, or
# prints nothing and returns 1 on any read/parse/timeout failure. TARGET is
# empty for "read this host directly" or an ssh(1) target string for a
# bounded remote read. Deterministic, network-free overrides for tests (each a
# "<load1> <nproc>" pair -- the ratio is still computed from it, never
# hardcoded):
#   PREPUSH_LOAD_OVERRIDE_LOCAL   overrides the direct (TARGET="") read
#   PREPUSH_LOAD_OVERRIDE_REMOTE  overrides every ssh-target read
host_load_ratio() {
  local target="$1" raw load1 ncpu timeout_cmd
  if [ -z "$target" ]; then
    if [ -n "${PREPUSH_LOAD_OVERRIDE_LOCAL:-}" ]; then
      raw="$PREPUSH_LOAD_OVERRIDE_LOCAL"
    else
      raw="$(python3 -c "$_PREPUSH_LOAD_PROBE_PY" 2> /dev/null)" || return 1
    fi
  else
    if [ -n "${PREPUSH_LOAD_OVERRIDE_REMOTE:-}" ]; then
      raw="$PREPUSH_LOAD_OVERRIDE_REMOTE"
    else
      timeout_cmd="$(_prepush_timeout_cmd)"
      if [ -n "$timeout_cmd" ]; then
        raw="$("$timeout_cmd" 6 ssh -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
          "$target" "python3 -c '${_PREPUSH_LOAD_PROBE_PY}'" 2> /dev/null)" || return 1
      else
        raw="$(ssh -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
          "$target" "python3 -c '${_PREPUSH_LOAD_PROBE_PY}'" 2> /dev/null)" || return 1
      fi
    fi
  fi
  [ -n "$raw" ] || return 1
  # shellcheck disable=SC2086
  set -- $raw
  load1="${1:-}"
  ncpu="${2:-}"
  [ -n "$load1" ] && [ -n "$ncpu" ] && [ "$ncpu" != "0" ] || return 1
  awk -v l="$load1" -v n="$ncpu" 'BEGIN { if (n + 0 <= 0) exit 1; printf "%s %s %.3f\n", l, n, (l / n) }'
}

# host_is_fit TARGET -- 0 if measured load1/nproc is at/under
# PREPUSH_LOAD_THRESHOLD, 1 if over threshold, 2 if the read itself failed
# (unreachable/unresolvable). Callers must not conflate 1 and 2 anywhere the
# difference is user-visible ("over capacity" vs "could not check").
host_is_fit() {
  local target="$1" ratio
  ratio="$(host_load_ratio "$target" | awk '{print $3}')" || return 2
  [ -n "$ratio" ] || return 2
  awk -v r="$ratio" -v thr="$PREPUSH_LOAD_THRESHOLD" 'BEGIN { exit !(r <= thr + 0) }'
}

guard_full_suite_host() {
  local host lc_host lc_target lc_201 heavy_what
  # OMN-15408: the caller names WHICH heavyweight run is being guarded, so the
  # refusal names the real cause. Default preserves the OMN-15059 wording for
  # the flag-driven escalation call sites, which pass no argument.
  heavy_what="${1:-heavy fail-closed full-suite escalation}"
  host="$(hostname -s 2>/dev/null || true)"
  if [ -z "$host" ]; then
    # Fail CLOSED (OMN-16489): see the routing note above PREPUSH_200_HOSTNAME.
    die "could not determine the local hostname while deciding where ${heavy_what} may run" \
        "heavy gate runs are routed by host identity (OMN-15059) and an unidentifiable host cannot be routed. Fix 'hostname -s' (macOS: 'sudo scutil --set HostName <name>'; Linux: 'hostnamectl set-hostname <name>'), or run the push from a designated gate host (.200 '${PREPUSH_200_HOSTNAME}' or the .201 gate-runner '${PREPUSH_201_GATE_RUNNER_HOSTNAME}')"
  fi
  lc_host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  lc_target="$(printf '%s' "$PREPUSH_200_HOSTNAME" | tr '[:upper:]' '[:lower:]')"
  lc_201="$(printf '%s' "$PREPUSH_201_GATE_RUNNER_HOSTNAME" | tr '[:upper:]' '[:lower:]')"
  if [ "$lc_host" = "$lc_target" ] || [ "$lc_host" = "$lc_201" ]; then
    # OMN-16295: identity alone is not enough -- this known-good host must
    # also have capacity right now.
    if host_is_fit ""; then
      return 0
    fi
    if [ -n "${PREPUSH_ALLOW_LOCAL_FULL_SUITE:-}" ]; then
      log "WARNING: DEGRADED-CAPACITY OVERRIDE IN EFFECT (PREPUSH_ALLOW_LOCAL_FULL_SUITE set) -- running ${heavy_what} on '${host}' at/over the ${PREPUSH_LOAD_THRESHOLD}x-core load threshold. Treat any evidence from this run as WEAKER than a fit-host-run gate."
      return 0
    fi
    local other_target other_label other_rc other_note
    if [ "$lc_host" = "$lc_target" ]; then
      other_target="$PREPUSH_201_SSH_TARGET"
      other_label="the .201 gate-runner (${PREPUSH_201_GATE_RUNNER_HOSTNAME})"
    else
      other_target="$PREPUSH_200_SSH_TARGET"
      other_label=".200 (${PREPUSH_200_HOSTNAME})"
    fi
    other_rc=0
    host_is_fit "$other_target" || other_rc=$?
    case "$other_rc" in
      0) other_note="${other_label} currently HAS capacity -- route there instead" ;;
      2) other_note="${other_label} could not be reached to check capacity" ;;
      *) other_note="${other_label} is ALSO at/over the load threshold" ;;
    esac
    die "${heavy_what} triggered on '${host}' (the designated host by identity), but its load is at/over the ${PREPUSH_LOAD_THRESHOLD}x-core threshold" \
        "${other_note}. See docs/runbooks/200-build-lane-execution-pattern.md for the .201 gate-runner recipe, or set PREPUSH_ALLOW_LOCAL_FULL_SUITE=1 to run here anyway (degraded evidence -- do not use as a routine bypass)"
  fi
  if [ -n "${PREPUSH_ALLOW_LOCAL_FULL_SUITE:-}" ]; then
    log "WARNING: DEGRADED-HOST OVERRIDE IN EFFECT (PREPUSH_ALLOW_LOCAL_FULL_SUITE set) -- running ${heavy_what} on '${host}', NOT the designated .200 host ('${PREPUSH_200_HOSTNAME}'). This host has weaker isolation/headroom than .200; treat any evidence from this run as WEAKER than a .200-run gate. See docs/runbooks/200-build-lane-execution-pattern.md."
    return 0
  fi
  die "${heavy_what} triggered on host '${host}', not the designated .200 build host ('${PREPUSH_200_HOSTNAME}')" \
      "push from .200 instead (ssh jonah@stickybeatz-studio.tail75df5e.ts.net, wrap remote commands as zsh -lc \"...\"; see docs/runbooks/200-build-lane-execution-pattern.md for the full pattern), OR set PREPUSH_ALLOW_LOCAL_FULL_SUITE=1 to run the full suite on this host anyway (visible, degraded-evidence override -- do not use as a routine bypass)"  # onex-allow-internal-ip OMN-16156 reason="operator-facing error message needs the real remote-push hostname to be actionable"
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

# =============================================================================
# Override-inheritance sanitization (OMN-16489, F-04)
# =============================================================================
# PREPUSH_* overrides (and ENABLE_SMART_TESTS) are honored at THIS hook's
# entry only. They must never inherit into the pytest subprocess tree: a test
# down there that re-invokes this script would receive the OUTER push's bypass
# grants -- the exact mechanism that turned one sanctioned override into a
# recursive 44k-test full-suite launcher (friction report F-01/F-04, ~9h03m).
# Called inside the subshell wrapping each pytest invocation, after the
# command's own knobs have been captured into non-PREPUSH names, so the parent
# hook's variables are untouched. Only EXPORTED names can inherit, so only
# those are scrubbed. ONEX_PREPUSH_HOOK_ACTIVE deliberately survives -- the
# recursion guard above depends on children inheriting it. This stops
# inheritance ONLY; override semantics at hook entry are unchanged (the
# override-mechanism redesign is OMN-16480, review-gated).
scrub_prepush_override_env() {
  local v
  for v in $(compgen -A export PREPUSH_ || true); do
    unset "$v" || true
  done
  unset ENABLE_SMART_TESTS || true
}

if [ "$IS_FULL" = "True" ] || [ "$IS_FULL" = "true" ]; then
  guard_full_suite_host
  log "running FULL suite (fail-closed escalation): uv run pytest ${FULL_SUITE_TARGET} --ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
  (
    _pytest_extra_args="${PREPUSH_PYTEST_ARGS:-}"
    scrub_prepush_override_env
    # shellcheck disable=SC2086
    exec uv run pytest "${FULL_SUITE_TARGET}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${_pytest_extra_args}
  ) || RC=$?
elif [ "${#PATHS[@]}" -gt 0 ]; then
  # OMN-15408: guard on the SELECTED WORK, not the is_full_suite flag. A
  # selection that covers the whole full-suite target is the heavy run under
  # another name and must be routed to .200 exactly as the flagged escalation is.
  if selection_is_whole_suite "$FULL_SUITE_TARGET" "${PATHS[@]}"; then
    guard_full_suite_host "whole-suite-equivalent impacted selection (is_full_suite=${IS_FULL}, selected paths [ ${PATHS_STR}] cover the entire '${FULL_SUITE_TARGET}' escalation target)"
  fi
  log "running impacted subset: uv run pytest ${PATHS_STR}--ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
  (
    _pytest_extra_args="${PREPUSH_PYTEST_ARGS:-}"
    scrub_prepush_override_env
    # shellcheck disable=SC2086
    exec uv run pytest "${PATHS[@]}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${_pytest_extra_args}
  ) || RC=$?
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
