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
# Inheritable env-var gate overrides are REJECTED AT ENTRY (OMN-16480)
# =============================================================================
# Ported to this repo by OMN-17435, and deliberately BEFORE the lab-dispatch
# picker below rather than after it. Porting the picker first would add a new
# PASS path to this gate with no entry rejection behind it, so the two land in
# the same commit with the rejection reachable first. (Same ordering OMN-17159
# used for omnibase_core; the reason is identical.)
#
# This gate's escape hatch used to BE an environment variable
# (`PREPUSH_ALLOW_LOCAL_FULL_SUITE=1`). An environment variable is inherited by
# every descendant process, is bound to no repo/commit/run, never expires, and
# leaves no receipt -- so "permission to bypass the load gate once, for this
# push" was really "permission for every process this shell ever spawns to
# bypass this gate, silently". Same failure shape Rule 10 was hardened against
# for `[skip-*` tokens (OMN-9731 / OMN-13388), one layer down.
#
# Measured: on 2026-08-23 that variable leaked from an operator shell into a
# guard test's `env=dict(os.environ)` subprocess copy; the sibling hook took its
# degraded-override branch and recursively launched another full 44,064-test
# suite, which reached the same test and recursed again -- ~9h03m, ~72% of all
# serialized suite wall-clock in that window (friction report F-01/F-04).
# Compliance was PERFECT that night: zero `[skip-*`, zero `--no-verify`. The
# damage came from the sanctioned escape path being used correctly.
#
# So the variable is no longer an arming signal in either direction: its
# presence is a HARD REFUSAL, not a bypass. That is what makes inheritance
# harmless -- a leaked override can no longer arm anything, and it surfaces
# immediately instead of silently disarming the gate for a whole process tree.
# The supported path is a single-use, repo+HEAD-scoped, TTL-bounded, receipted
# grant token: ***REDACTED***
#
# Matched by PREFIX, not by one exact name, so a future
# `PREPUSH_ALLOW_SOMETHING_ELSE` cannot quietly reopen the class.
reject_inherited_env_overrides() {
  local leaked
  leaked="$(env | sed -n 's/^\(PREPUSH_ALLOW_[A-Za-z0-9_]*\)=..*/\1/p' | sort -u | tr '\n' ' ')"
  leaked="${leaked% }"
  [ -n "$leaked" ] || return 0
  die "inheritable gate-override environment variable(s) present: ${leaked} -- these are REJECTED, never honored (OMN-16480)" \
      "unset them in this shell (e.g. \`unset ${leaked%% *}\`), then, if this run genuinely must proceed on this host, mint a scoped single-use grant: \`uv run python scripts/hooks/prepush_override_grant.py mint --reason '<why>'\`. The grant is bound to this repo and this HEAD sha, expires in minutes, is consumed by the first guard that reads it (so no child process can reuse it), and appends a receipt line to .onex_state/prepush_override/receipts.jsonl"
}
reject_inherited_env_overrides

# consume_override_grant CONTEXT -- 0 when a valid single-use grant was claimed
# for this run, 1 otherwise. Delegates to the one implementation
# (scripts/hooks/prepush_override_grant.py) that the pytest-side guard also
# uses, so the two entry points can never drift apart on what a valid grant is.
# Routed through `uv run` per the OMN-14953 pinned-interpreter gate.
consume_override_grant() {
  uv run python "${REPO_ROOT}/scripts/hooks/prepush_override_grant.py" \
    consume --context "$1"
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

# Cross-platform (Linux `.201` / macOS `.200`) load probe, printing
# "<load1> <nproc>". Deliberately interpreter-free (OMN-17435, matching the
# OMN-16991 shape already shipped in omnibase_infra): the previous form here
# ran `python3 -c` on BOTH the local and the ssh branch, which OMN-14953's
# pinned-interpreter doctrine wants routed through `uv run` -- and the ssh
# branch cannot, because `.201` has no `uv` binary at all (probed 2026-08-20,
# re-probed 2026-09-01). Dropping the interpreter satisfies that constraint
# rather than carving an exception out of it, and keeps interpreter startup
# off the pre-push critical path. It also makes the probe usable on a host
# with no python3 on its non-interactive PATH, which is the normal shape of an
# ssh login session on the lab Macs.
#
# Two portability constraints, both load-bearing:
#   1. Field extraction uses cut(1), NOT `set -- $(...)` word splitting.
#      `.200`'s remote login shell is zsh, which does not word-split unquoted
#      command substitution, so `set --` would collapse the whole line into $1
#      there while working fine on `.201`'s bash.
#   2. This snippet is handed to ssh(1) as the remote command and executed by
#      whatever login shell the remote user has, so it stays POSIX and carries
#      no single quotes (it is itself a single-quoted assignment here).
# shellcheck disable=SC2016  # intentionally unexpanded: evaluated by the local
# `sh -c` / the remote login shell, not by this script.
#
# THIRD FIELD: AVAILABLE MEMORY IN MiB (OMN-17392, the OMN-17271 memory
# dimension). load1 is a CPU-time proxy and says nothing about the resource
# that actually killed a suite: on 2026-08-31 an OMN-17316 landing lost hours
# to the `.201` gate-runner OOM-killing full suites at its 8 GiB cap while this
# picker -- reading CPU only -- kept ranking `.201` FIRST. Measured live while
# building this change, one second apart:
#
#   .201 HOST:      load 3.27 / 32 cores = 0.10x   mem_avail 49771 MiB
#   gate-runner:    load 3.27 / 32 cores = 0.10x   mem_avail  2562 MiB
#                   (/sys/fs/cgroup/memory.max 8589934592
#                    - memory.current 5902548992)
#
# Identical load, 19x difference in the resource that OOMs. A CPU-only probe
# cannot tell those two apart, which is exactly why the picker kept
# recommending a saturated target.
#
# CGROUP-AWARE ON PURPOSE: inside a memory-capped container the machine's
# MemAvailable is not the headroom the suite gets, so the probe reports
# min(MemAvailable, memory.max - memory.current) and a capped container
# advertises its OWN cap. Both cgroup v2 (memory.max/current) and v1
# (memory.limit_in_bytes/usage_in_bytes) are read; an uncapped v1 limit is a
# huge sentinel, hence the 1e12 guard.
#
# `-1` means COULD NOT READ, and the picker treats it as unfit -- never as
# ample. Silence is not headroom (the posture the load probe already had).
#
# awk is deliberately NOT used for the memory read even though it is used
# below: every awk program here would need single quotes, and this snippet is
# itself a single-quoted assignment. POSIX arithmetic + cut/grep/tr carry no
# single quotes and need no second quoting level. Verified live on all four
# lab hosts plus the capped container (macOS vm_stat path and Linux
# /proc/meminfo path both).
# shellcheck disable=SC2016  # intentionally unexpanded: evaluated by the local
# `sh -c` / the remote login shell, not by this script.
_PREPUSH_LOAD_PROBE_SH='n=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)
[ "$n" -gt 0 ] || exit 1
if [ -r /proc/loadavg ]; then
  l=$(cut -d" " -f1 /proc/loadavg)
else
  l=$(sysctl -n vm.loadavg 2>/dev/null | cut -d" " -f2)
fi
[ -n "$l" ] || exit 1
m=-1
if [ -r /proc/meminfo ]; then
  k=$(grep MemAvailable /proc/meminfo | tr -s " " | cut -d" " -f2)
  [ -n "$k" ] && m=$((k / 1024))
  if [ -r /sys/fs/cgroup/memory.max ] && [ -r /sys/fs/cgroup/memory.current ]; then
    x=$(cat /sys/fs/cgroup/memory.max)
    c=$(cat /sys/fs/cgroup/memory.current)
    if [ "$x" != max ] && [ -n "$c" ]; then
      h=$(((x - c) / 1048576))
      [ "$h" -lt "$m" ] && m=$h
    fi
  elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ] && [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then
    x=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)
    c=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes)
    if [ -n "$x" ] && [ -n "$c" ] && [ "$x" -lt 1000000000000 ]; then
      h=$(((x - c) / 1048576))
      [ "$h" -lt "$m" ] && m=$h
    fi
  fi
else
  v=$(vm_stat 2>/dev/null) || v=""
  if [ -n "$v" ]; then
    p=$(printf "%s\n" "$v" | grep "page size of" | tr -dc "0-9")
    f=$(printf "%s\n" "$v" | grep "Pages free" | tr -dc "0-9")
    i=$(printf "%s\n" "$v" | grep "Pages inactive" | tr -dc "0-9")
    s=$(printf "%s\n" "$v" | grep "Pages speculative" | tr -dc "0-9")
    u=$(printf "%s\n" "$v" | grep "Pages purgeable" | tr -dc "0-9")
    [ -n "$p" ] && [ -n "$f" ] && m=$(((f + ${i:-0} + ${s:-0} + ${u:-0}) * p / 1048576))
  fi
fi
printf "%s %s %s\n" "$l" "$n" "$m"'

# Prefer GNU coreutils timeout(1); fall back to gtimeout(1) (Homebrew name on
# macOS); fall back to no wrapper at all (ssh -o ConnectTimeout already bounds
# the connection phase, and the remote command is a single fast shell probe).
_prepush_timeout_cmd() {
  if command -v timeout > /dev/null 2>&1; then
    printf 'timeout'
  elif command -v gtimeout > /dev/null 2>&1; then
    printf 'gtimeout'
  fi
}

# `ssh -n` IS LOAD-BEARING, not hygiene (OMN-16991 verify finding 1). This probe
# is called from inside the host-table row loop in pick_capacity_host, whose
# stdin is the row list. Without -n, ssh(1) reads and discards that stdin, so
# the FIRST probe swallowed every remaining row and the picker evaluated
# exactly one host -- live, it probed h200 and never saw h201/h101/h105.
# host_load_ratio TARGET -- prints "<load1> <nproc> <ratio>" and returns 0, or
# prints nothing and returns 1 on any read/parse/timeout failure. TARGET is
# empty for "read this host directly" or an ssh(1) target string for a
# bounded remote read. Deterministic, network-free overrides for tests (each a
# "<load1> <nproc>" pair -- the ratio is still computed from it, never
# hardcoded):
#   PREPUSH_LOAD_OVERRIDE_LOCAL   overrides the direct (TARGET="") read
#   PREPUSH_LOAD_OVERRIDE_REMOTE  overrides every ssh-target read
host_load_ratio() {
  local target="$1" raw load1 ncpu memmb timeout_cmd
  # OMN-16995: REAP FIRST, MEASURE SECOND. A leaked `sh -c while :; do :; done`
  # orphan is indistinguishable from real work in load1, and 19 of them once
  # put `.200` at 1.64x-core and refused every heavy escalation in the lab. The
  # reaper is defined in prepush_dispatch.sh, which is sourced below this
  # definition and therefore resolved by the time any caller runs.
  reap_spin_loop_orphans "$target" || true
  if [ -z "$target" ]; then
    if [ -n "${PREPUSH_LOAD_OVERRIDE_LOCAL:-}" ]; then
      raw="$PREPUSH_LOAD_OVERRIDE_LOCAL"
    else
      raw="$(sh -c "$_PREPUSH_LOAD_PROBE_SH" 2> /dev/null)" || return 1
    fi
  else
    if [ -n "${PREPUSH_LOAD_OVERRIDE_REMOTE:-}" ]; then
      raw="$PREPUSH_LOAD_OVERRIDE_REMOTE"
    else
      timeout_cmd="$(_prepush_timeout_cmd)"
      if [ -n "$timeout_cmd" ]; then
        raw="$("$timeout_cmd" 6 ssh -n -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
          "$target" "$_PREPUSH_LOAD_PROBE_SH" 2> /dev/null)" || return 1
      else
        raw="$(ssh -n -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
          "$target" "$_PREPUSH_LOAD_PROBE_SH" 2> /dev/null)" || return 1
      fi
    fi
  fi
  [ -n "$raw" ] || return 1
  # shellcheck disable=SC2086
  set -- $raw
  load1="${1:-}"
  ncpu="${2:-}"
  # Third field is available MiB (OMN-17392). An override that supplies only
  # the historical "<load1> <nproc>" pair reports -1, i.e. "could not read",
  # and is therefore treated as UNFIT rather than ample -- the same posture the
  # real probe takes when /proc/meminfo and vm_stat are both unreadable. An
  # override is a test seam, not an exemption from the memory floor.
  memmb="${3:--1}"
  case "$memmb" in '' | *[!0-9-]*) memmb=-1 ;; esac
  [ -n "$load1" ] && [ -n "$ncpu" ] && [ "$ncpu" != "0" ] || return 1
  awk -v l="$load1" -v n="$ncpu" -v m="$memmb" \
    'BEGIN { if (n + 0 <= 0) exit 1; printf "%s %s %.3f %s\n", l, n, (l / n), m }'
}

# The floor, in MiB, of available memory a host must PROVE before it may take a
# heavy suite (OMN-17392). A DELIBERATE CONSTANT, not `${VAR:-4096}`: an env
# indirection here would be a one-word bypass of the exact admission control
# this adds (`PREPUSH_MIN_FREE_MEM_MB=0` restores the blind picker), and the
# operator directive is explicit that PREPUSH_* overrides stay forbidden.
# Tests drive the MEASUREMENT through PREPUSH_MEM_OVERRIDE_MAP, never the floor.
#
# 4096 is chosen against the measured OOM, not picked round: the `.201`
# gate-runner OOM-killed full suites at a 8 GiB cap (OMN-17247), and its
# headroom while running one measured 2562 MiB. A 4 GiB floor refuses that
# container while it is saturated and re-admits it once the suite drains,
# while keeping every host the lab actually uses in the fleet -- measured the
# same minute: h200 77564, h201 49771, h105 14664, h101 7459. A floor at 8192
# would have excluded h101, shrinking the fleet and pushing work back onto the
# Mac, which is the opposite of what this change is for.
PREPUSH_MIN_FREE_MEM_MB=4096

# host_is_fit TARGET -- 0 if the host proved BOTH capacity dimensions, 1 if it
# is measurably over on either, 2 if the read itself failed
# (unreachable/unresolvable/unreadable memory). Callers must not conflate 1 and
# 2 anywhere the difference is user-visible ("over capacity" vs "could not
# check"). Sets PREPUSH_LAST_FIT_DETAIL so a caller can say WHICH dimension
# refused instead of reporting a bare "unfit".
host_is_fit() {
  local target="$1" reading ratio memmb
  PREPUSH_LAST_FIT_DETAIL=""
  reading="$(host_load_ratio "$target")" || return 2
  ratio="$(printf '%s' "$reading" | awk '{print $3}')"
  memmb="$(printf '%s' "$reading" | awk '{print $4}')"
  [ -n "$ratio" ] || return 2
  if ! awk -v r="$ratio" -v thr="$PREPUSH_LOAD_THRESHOLD" 'BEGIN { exit !(r <= thr + 0) }'; then
    PREPUSH_LAST_FIT_DETAIL="load ${ratio}x > ${PREPUSH_LOAD_THRESHOLD}x"
    return 1
  fi
  # Memory is checked AFTER load and reported separately: a host that is idle
  # but memory-starved is the case the CPU-only picker got wrong, and calling
  # it "over capacity" without naming memory would send the reader hunting for
  # CPU load that is not there.
  if [ -z "$memmb" ] || [ "$memmb" = "-1" ]; then
    PREPUSH_LAST_FIT_DETAIL="memory unreadable"
    return 2
  fi
  if [ "$memmb" -lt "$PREPUSH_MIN_FREE_MEM_MB" ] 2> /dev/null; then
    PREPUSH_LAST_FIT_DETAIL="mem ${memmb}MiB < ${PREPUSH_MIN_FREE_MEM_MB}MiB"
    return 1
  fi
  PREPUSH_LAST_FIT_DETAIL="load ${ratio}x, mem ${memmb}MiB"
  return 0
}

# =============================================================================
# Lab-wide distribution helpers (OMN-16991, ported by OMN-17435)
# =============================================================================
# Sourced AFTER host_load_ratio/host_is_fit/_prepush_timeout_cmd, which the
# library reuses rather than reimplementing, and BEFORE guard_full_suite_host,
# which is its only caller. Located relative to this script so it resolves the
# same way whether git invokes the hook through .git/hooks or core.hooksPath.
#
# The file is a BYTE-FOR-BYTE copy of omnibase_infra's
# scripts/hooks/prepush_dispatch.sh. That is enforced, not merely intended:
# tests/scripts/test_prepush_host_table.py pins its sha256 AND the upstream
# revision it was copied from (recorded in scripts/hooks/prepush_dispatch.upstream),
# so a local edit that silently forks the picker fails this repo's own suite.
# shellcheck source=scripts/hooks/prepush_dispatch.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prepush_dispatch.sh"

# REMOTE_LAB_RUN_VERDICT (OMN-16991) -- set to 1 when a designated lab host ran
# this exact tree green over the remote leg, so the caller elides the local
# pytest entirely.
REMOTE_LAB_RUN_VERDICT=0


# dispatch_to_lab_host HEAVY_WHAT -- try to satisfy HEAVY_WHAT by running it on
# a designated lab host, cheapest-loaded first.
# 0 = satisfied (green), 1 = no evidence (caller falls through), and it does
# NOT return on a remote RED: a suite that genuinely failed on a designated
# host is a failing gate, so it refuses here rather than letting the caller
# fall through to the degraded-evidence override grant.
#
# It walks the RANKED candidate list rather than betting the whole escalation on
# one host (OMN-16991 verify finding 3). Only a verdict -- green or red -- ends
# the walk. "No evidence" (unreachable on arrival, no completion marker) and
# "slot taken between the probe and the run" (rc 4) are placement misses, not
# statements about the tree, so they advance to the next fit host instead of
# refusing a push that another idle lab host could have cleared.
#
# `authorizing` is passed EXPLICITLY: this is the verdict-bearing path, and a
# shadow row's verdict cannot satisfy the escalation by definition. Ranking one
# in would spend a bundle, an scp, a `uv sync` and a full suite to produce an
# answer that is then thrown away, while the authorizing host that could have
# answered goes unprobed.
dispatch_to_lab_host() {
  local heavy_what repo rc=0 idx=1 total
  heavy_what="$1"
  repo="$(basename "$REPO_ROOT")"
  if ! pick_capacity_host "$PREPUSH_LC_HOST" "$repo" authorizing; then
    log "no lab host is fit for ${heavy_what}: ${PREPUSH_PROBE_LOG:-no hosts probed}"
    return 1
  fi
  total="$(prepush_candidate_count)"
  while [ "$idx" -le "$total" ]; do
    prepush_select_candidate "$idx" || break
    if [ -z "$PREPUSH_PICK_SSH" ]; then
      # This candidate IS this host: there is nothing to DISTRIBUTE, so the
      # remote leg cannot answer for it and the ranked hosts after it still
      # can. Skipping it here is correct -- but it used to be SILENT, and that
      # silence is how OMN-17280 stayed invisible: for an actor who can reach
      # no other host, this was the only fit candidate in the lab, and the walk
      # dropped it without a word before falling through to die(). The
      # same-host route now lives in prepush_local_actor_route, one rung below
      # this call in guard_full_suite_host; naming the skip makes the transcript
      # explain how control got there.
      log "lab placement: ${PREPUSH_PICK_LABEL} IS this host, so it carries no remote leg; the same-host route is evaluated after the lab walk (OMN-17280)"
      idx=$((idx + 1))
      continue
    fi
    rc=0
    prepush_remote_run "$heavy_what" || rc=$?
    case "$rc" in
      0)
        REMOTE_LAB_RUN_VERDICT=1
        return 0
        ;;
      3)
        die "${heavy_what} FAILED on the designated lab host '${PREPUSH_PICK_HOSTNAME}' (${PREPUSH_PICK_LABEL})" \
            "the suite genuinely failed on a host we designated -- this is a red gate, not a capacity problem. Read the streamed [${PREPUSH_PICK_LABEL}] output above (the tail of that host's suite.log is printed there), fix the failing tests, then re-push. A remote red is never satisfied by minting an override grant"
        ;;
      4)
        log "lab placement: ${PREPUSH_PICK_LABEL}'s heavy-suite slot was taken on arrival; trying the next fit host"
        ;;
      *)
        log "lab placement: ${PREPUSH_PICK_LABEL} returned no usable evidence; trying the next fit host"
        ;;
    esac
    idx=$((idx + 1))
  done
  log "no fit lab host produced a verdict for ${heavy_what}: ${PREPUSH_PROBE_LOG:-no hosts probed}"
  return 1
}

# =============================================================================
# Off-box-by-default routing (OMN-17392)
# =============================================================================
# Operator directive 2026-08-31, verbatim: "we should move prepush off this box
# if possible". The box is `.200` (row h200), and the reason the directive was
# needed is a single short-circuit below: the guard ran the heavy suite LOCALLY
# the moment the local host was a designated capacity row and its load probe
# read under threshold. Lab dispatch was only ever reached once the local host
# was already over threshold -- i.e. the fleet was consulted only after this
# machine was too loaded to be worth consulting it about. Measured that day:
# load1 96.58 / 24 cores = 4.02x during landings, with h105 at 0.12x and h201
# at 0.10x sitting idle.
#
# The budget and interval are CONSTANTS, not `${VAR:-...}`. An env indirection
# would be a one-word bypass of exactly this policy (`..._BUDGET=0` collapses
# straight through to the local fallback), and the directive is explicit that
# PREPUSH_* overrides stay forbidden. Tests pass the budget positionally.
PREPUSH_OFFBOX_WAIT_BUDGET_SECONDS=900
PREPUSH_OFFBOX_WAIT_INTERVAL_SECONDS=60

# prepush_try_local_heavy_slot -- 0 when this host has PROVEN both capacity and
# an exclusive heavy-suite slot (and now holds it), 1 otherwise. Factored out of
# guard_full_suite_host unchanged in substance so the `allowed` path and the
# post-wait fallback share one implementation and cannot drift into two
# different notions of "may run here".
#
# It records WHY it said no in PREPUSH_LOCAL_HEAVY_REASON. Before this function
# existed the refusal was logged from inside an `if host_is_fit ""` branch, so
# "this host is fit but its slot is held" was necessarily true wherever it
# printed. Hoisting the check into here made that sentence reachable for an
# over-loaded or memory-starved host too, where it is measurably false and
# sends the reader hunting for a held lock that does not exist.
prepush_try_local_heavy_slot() {
  local lw lock_rc=0
  PREPUSH_LOCAL_HEAVY_REASON=""
  if ! host_is_fit ""; then
    PREPUSH_LOCAL_HEAVY_REASON="this host is not fit (${PREPUSH_LAST_FIT_DETAIL:-unmeasured})"
    return 1
  fi
  lw="$(prepush_local_workroot "$PREPUSH_LC_HOST" || true)"
  [ -n "$lw" ] || lw="${REPO_ROOT}/.onex_state/prepush_distribution"
  prepush_lock_acquire "$lw" || lock_rc=$?
  if [ "$lock_rc" -eq 0 ]; then
    # No `trap ... EXIT` here: prepush_hook_cleanup (installed once, below)
    # already releases the lock. Installing a second EXIT trap would drop the
    # temp-file cleanup this hook installed first.
    return 0
  fi
  if [ "$lock_rc" -eq 2 ]; then
    # The workroot is unusable, which says nothing about this host's capacity.
    # Proceed exactly as the hook did before this lock existed rather than
    # inventing a refusal out of an infrastructural failure.
    # OMN-17280. Before degrading to an UNSERIALIZED run, ask whether this is
    # the actor case: a workroot we cannot write is the signature of running as
    # someone other than whoever provisioned this host, and when NO capacity row
    # is reachable for that actor the same-host route is the governed answer --
    # it takes a per-actor slot under $HOME instead of running with no lock at
    # all, and it writes the receipt that names why the suite ran here. It
    # declines the moment any lab host is reachable, so an OWNER whose workroot
    # is genuinely broken still gets exactly the warning below.
    if prepush_local_actor_route "${heavy_what:-heavy fail-closed full-suite escalation}" \
      "$(prepush_identity_label "$PREPUSH_LC_HOST" || true)"; then
      return 0
    fi
    log "WARNING: could not create the heavy-suite slot lock under '${lw}' -- running unserialized on this host (pre-OMN-16991 behavior). Fix the workroot to restore serialization (OMN-16174)."
    return 0
  fi
  PREPUSH_LOCAL_HEAVY_REASON="this host is fit (${PREPUSH_LAST_FIT_DETAIL:-unmeasured}) but its heavy-suite slot is already held"
  return 1
}

# prepush_lab_has_transient_capacity -- 0 when the last probe refused at least
# one candidate for a reason that CAN resolve on its own, 1 when every refusal
# is structural.
#
# The bounded wait below exists to catch a lab slot freeing up. That premise
# holds for `busy` (a suite finishes), `over` (load drains) and `mem-over` (the
# suite holding the memory exits). It does NOT hold for `unreachable`,
# `repo-denied`, `disabled`, `uv-unfit` or `mode-*-not-eligible`: none of those
# change because a pusher waited, so spending the budget on them buys nothing
# and costs 900s of silence before the fallback the push was always going to
# reach. The concrete case is a Mac off the lab LAN -- every remote row probes
# `unreachable`, and without this gate EVERY heavy push there pays the full
# budget before running locally anyway.
#
# This can only SHORTEN a wait, never skip a gate: the caller still returns "no
# placement", and the local fallback it falls through to still has to prove
# measured capacity AND an exclusive slot. It matches on the probe-log tokens
# pick_capacity_host writes, so a new refusal reason defaults to STRUCTURAL --
# a reason we have not classified does not silently earn a 15-minute wait.
prepush_lab_has_transient_capacity() {
  case "${PREPUSH_PROBE_LOG:-}" in
    *"=busy("* | *"=over("* | *"=mem-over("*) return 0 ;;
  esac
  return 1
}

# prepush_wait_for_lab_capacity HEAVY_WHAT BUDGET INTERVAL -- retry lab
# placement until a host takes the work or BUDGET seconds elapse. 0 = a lab host
# produced a green verdict, 1 = the budget is exhausted with no placement.
#
# This is the "queue and wait" rung the directive asks for, and it is VISIBLE by
# construction: every attempt logs the probe trail it just took, how much of the
# budget it has spent, and when it will re-probe. A push that waits looks like a
# push that is waiting, not like a hung hook.
#
# It re-probes the WHOLE ranked list each round rather than re-trying one host:
# the thing being waited on is a slot freeing up ANYWHERE in the lab, and by the
# next round the ranking has usually changed.
#
# It does NOT catch a remote RED: dispatch_to_lab_host die()s on a genuine
# failure, so a red suite still refuses the push immediately instead of being
# retried until the budget runs out.
prepush_wait_for_lab_capacity() {
  local heavy_what="$1" budget="$2" interval="$3" waited=0 attempt=1
  while :; do
    if dispatch_to_lab_host "$heavy_what"; then
      return 0
    fi
    [ "$waited" -lt "$budget" ] || break
    if ! prepush_lab_has_transient_capacity; then
      log "OFF-BOX QUEUE-AND-WAIT: not waiting -- every lab refusal cannot resolve on its own."
      log "  probed: ${PREPUSH_PROBE_LOG:-none}"
      log "  No host is merely busy/over/memory-starved, so re-probing would return the same answer for the full ${budget}s. Falling through to the refusal ladder now."
      return 1
    fi
    log "OFF-BOX QUEUE-AND-WAIT (attempt ${attempt}): no lab host has headroom for ${heavy_what} yet."
    log "  probed: ${PREPUSH_PROBE_LOG:-none}"
    log "  waited ${waited}s of a ${budget}s budget; re-probing the whole ranked list in ${interval}s. Ctrl-C aborts the push."
    sleep "$interval"
    waited=$((waited + interval))
    attempt=$((attempt + 1))
  done
  log "OFF-BOX QUEUE-AND-WAIT: ${budget}s budget exhausted after ${attempt} attempt(s); no lab host took ${heavy_what}."
  return 1
}


guard_full_suite_host() {
  local host lc_host label heavy_what designated policy
  # OMN-15408: the caller names WHICH heavyweight run is being guarded, so the
  # refusal names the real cause. Default preserves the OMN-15059 wording for
  # the flag-driven escalation call sites, which pass no argument.
  heavy_what="${1:-heavy fail-closed full-suite escalation}"
  host="$(hostname -s 2>/dev/null || true)"
  if [ -z "$host" ]; then
    # Fail CLOSED (OMN-16489): see the routing note above PREPUSH_200_HOSTNAME.
    die "could not determine the local hostname while deciding where ${heavy_what} may run" \
        "heavy gate runs are routed by host identity (OMN-15059) and an unidentifiable host cannot be routed. Fix 'hostname -s' (macOS: 'sudo scutil --set HostName <name>'; Linux: 'hostnamectl set-hostname <name>'), or run the push from a designated gate host listed in ${PREPUSH_HOST_TABLE_REL}"
  fi
  lc_host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  PREPUSH_LC_HOST="$lc_host"

  # OMN-16991: host identity now resolves against the COMMITTED host table
  # instead of the two hard-coded names this guard used to test
  # (`[ "$lc_host" = "$lc_target" ] || [ "$lc_host" = "$lc_201" ]`). That
  # literal `||` -- not policy -- was the entire structural reason .101 and
  # .105 could not be used, and it is also why `.201` only ever matched from
  # INSIDE the gate-runner container: the container sets hostname
  # gate-runner-201 while the host itself reports omninode-pc, so every push on
  # the host needed PREPUSH_201_GATE_RUNNER_HOSTNAME exported to pass. Both
  # names are now rows, so `.201` is designated intrinsically and no env var
  # has to survive a process or ssh boundary for the guard to see it.
  #
  # An UNREADABLE table fails CLOSED, on the same reasoning as the unresolvable
  # hostname above: heavy runs are routed by host identity, and identity that
  # cannot be resolved cannot be routed.
  if ! prepush_table_text > /dev/null 2>&1; then
    die "the pre-push host table (${PREPUSH_HOST_TABLE_REL}) could not be read from HEAD, so no host can be identified as a designated gate host for ${heavy_what}" \
        "the table is read from the COMMITTED tree so an uncommitted row cannot self-designate this machine as an authorizing gate host. Commit ${PREPUSH_HOST_TABLE_REL} (or, if you have edited it, commit the edit so HEAD and the working tree agree), then re-push"
  fi
  label="$(prepush_identity_label "$lc_host" || true)"
  designated="$(prepush_designated_hostnames)"

  if [ -n "$label" ]; then
    policy="$(prepush_heavy_local_policy "$lc_host" || true)"
    [ -n "$policy" ] || policy="allowed"

    if [ "$policy" = "prefer_remote" ]; then
      # OFF-BOX BY DEFAULT (OMN-17392). This host is a designated, authorizing
      # gate host and could very well be fit right now -- and that is exactly
      # the case the directive retires. Being ABLE to run the suite here is no
      # longer a reason to. The local run is still reachable, but only as the
      # LAST rung, after the lab has been asked and asked again.
      log "OFF-BOX ROUTING: '${host}' (${label}) is heavy_local=prefer_remote, so ${heavy_what} looks for a lab host BEFORE running here (OMN-17392)."
      if prepush_wait_for_lab_capacity "$heavy_what" \
        "$PREPUSH_OFFBOX_WAIT_BUDGET_SECONDS" "$PREPUSH_OFFBOX_WAIT_INTERVAL_SECONDS"; then
        return 0
      fi
      # The bounded wait is spent. Running here is now permitted -- but ONLY on
      # the same proof any other host must produce: measured capacity AND an
      # exclusive slot. A local host over threshold still refuses below exactly
      # as it did before this change, so this fallback is strictly narrower
      # than the pre-OMN-17392 behavior it replaces, never wider.
      if prepush_try_local_heavy_slot; then
        log "=============================================================================="
        log "LOCAL FALLBACK IN EFFECT -- ${heavy_what} is running ON THIS BOX ('${host}')."
        log "  This host is heavy_local=prefer_remote: off-box was tried FIRST and did not"
        log "  place. Waited the full ${PREPUSH_OFFBOX_WAIT_BUDGET_SECONDS}s off-box budget before falling back."
        log "  last probe: ${PREPUSH_PROBE_LOG:-none}"
        log "  local capacity accepted on: ${PREPUSH_LAST_FIT_DETAIL:-unknown}"
        log "  This is NOT a bypass: the full escalation runs here, unmodified. It is a"
        log "  capacity event -- if you are seeing it often, the lab is undersized or a"
        log "  host is wedged -- see the header of ${PREPUSH_HOST_TABLE_REL}."
        log "=============================================================================="
        return 0
      fi
      log "off-box placement failed and this host cannot take the work either -- ${PREPUSH_LOCAL_HEAVY_REASON:-no local capacity measured}; refusing rather than running a suite this host cannot support"
    else
      # OMN-16295: identity alone is not enough -- this known-good host must
      # also have capacity right now.
      #
      # OMN-16174/OMN-16991: the LOCAL heavy path took no lock of any kind
      # before that change, which is why five concurrent full suites once ran
      # on one host with one of them taking 97+ minutes. It is the busiest
      # path in the hook and was the only unserialized one. Take the same
      # exclusive slot a remote host would have to take.
      if prepush_try_local_heavy_slot; then
        return 0
      fi
      log "${PREPUSH_LOCAL_HEAVY_REASON:-this host cannot take the work}; looking for another lab host before refusing"
    fi
    # Precedence, in order of EVIDENCE STRENGTH -- not convenience:
    #   1. A designated lab host running this exact tree (OMN-16991). A real
    #      suite actually ran on hardware we designate, bound to this sha by a
    #      completion marker carrying {head_sha, argv_sha, exit, collected,
    #      log_sha256}.
    #   2. Single-use receipted degraded-capacity grant. Weakest: it runs a
    #      contended suite here and says so.
    #   3. die().
    #
    # omnibase_infra additionally consults a sha-pinned GitHub-hosted full-suite
    # run AHEAD of (1) (OMN-16688, `remote_full_suite_verified`). That leg is
    # NOT ported here -- it needs this repo's own CI shape wired into
    # prepush_remote_verify.py, which OMN-17159 also deferred for omnibase_core.
    # Its absence cannot make this gate accept less work: it only means this
    # repo has one fewer evidence source ABOVE the grant, never a new pass.
    #
    # OMN-17392 did NOT reorder this ladder -- it changed only WHEN a
    # `prefer_remote` host is allowed to skip it by running locally. On an
    # `allowed` host the ladder is reached exactly as before (local unfit or
    # slot held). On a `prefer_remote` host rung 1 has already been walked, and
    # a bounded wait spent on it, before control arrives here; re-walking it
    # costs one read-only probe sweep and is worth it, because the lab's state
    # may well have changed during a 900s wait.
    if dispatch_to_lab_host "$heavy_what"; then
      return 0
    fi
    # OMN-17280 -- SAME-HOST ROUTE, above the grant on evidence strength.
    # Placed here, and only here, so it can fire ONLY after the lab has been
    # asked and answered nothing. It refuses itself the moment any capacity row
    # is reachable for this actor, which is every one of the owner's own
    # pushes, so the OMN-17392 / OMN-17485 off-box preference is untouched. It
    # is above consume_override_grant because it produces a real full suite on
    # a designated authorizing host -- strictly stronger evidence than a
    # receipted degraded-capacity grant, and it burns no grant to get there.
    if prepush_local_actor_route "$heavy_what" "$label"; then
      return 0
    fi
    if consume_override_grant "degraded-capacity: ${heavy_what} on '${host}' at/over the ${PREPUSH_LOAD_THRESHOLD}x-core load threshold"; then
      log "WARNING: DEGRADED-CAPACITY OVERRIDE IN EFFECT (single-use grant consumed) -- running ${heavy_what} on '${host}' at/over the ${PREPUSH_LOAD_THRESHOLD}x-core load threshold. Treat any evidence from this run as WEAKER than a fit-host-run gate."
      return 0
    fi
    die "${heavy_what} triggered on '${host}' (designated gate host '${label}'), but its load is at/over the ${PREPUSH_LOAD_THRESHOLD}x-core threshold and no other lab host could take the work" \
        "probed hosts: ${PREPUSH_PROBE_LOG:-none}. The table's own header (${PREPUSH_HOST_TABLE_REL}) documents how to add or re-enable a lab host. Or mint a single-use grant to run here anyway (degraded evidence -- do not use as a routine bypass): uv run python scripts/hooks/prepush_override_grant.py mint --reason '<why>'"
  fi
  # Not a designated host. Same precedence, same ordering, same reasoning.
  if dispatch_to_lab_host "$heavy_what"; then
    return 0
  fi
  if consume_override_grant "degraded-host: ${heavy_what} on '${host}', not a designated gate host"; then
    log "WARNING: DEGRADED-HOST OVERRIDE IN EFFECT (single-use grant consumed) -- running ${heavy_what} on '${host}', NOT a designated gate host (${designated}). This host has weaker isolation/headroom; treat any evidence from this run as WEAKER than a designated-host gate. See ${PREPUSH_HOST_TABLE_REL} for the designated set."
    return 0
  fi
  die "${heavy_what} triggered on host '${host}', not the designated .200 build host ('${PREPUSH_200_HOSTNAME}') nor any other designated gate host (${designated})" \
      "probed lab hosts: ${PREPUSH_PROBE_LOG:-none}. Push from a designated host, OR add/enable a lab host (the procedure is in ${PREPUSH_HOST_TABLE_REL}'s header), OR mint a single-use override grant to run the full suite on this host anyway (visible, receipted, degraded-evidence override -- do not use as a routine bypass): uv run python scripts/hooks/prepush_override_grant.py mint --reason '<why>'"
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
# REPO_ROOT IS RESOLVED HERE, ABOVE THE EARLY CALL SITE, NOT BELOW IT
# (OMN-17435). It used to be resolved after this block, which was harmless
# while the guard only compared hostnames and ssh-probed load. It is not
# harmless now: `guard_full_suite_host` reads the host table with
# `git -C "$REPO_ROOT" show HEAD:...`, so under `set -u` an unset REPO_ROOT
# makes that read fail -- and the read's failure is (correctly) FAIL-CLOSED, so
# every `PREPUSH_FULL_SUITE=1` / `ENABLE_SMART_TESTS=off` escalation died with
# "the host table could not be read from HEAD" on a machine whose table was
# committed and clean. A refusal that names the wrong cause is worse than a
# refusal, because it sends the reader to commit a file that is already
# committed. Resolving the root first costs one `git rev-parse` and makes both
# call sites -- this static one and the dynamic one in the IS_FULL branch --
# read the same table the same way.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || die "not inside a git worktree" \
         "run 'git push' from within the omnimarket repository"
cd "$REPO_ROOT"

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
# ONE exit trap for the whole hook (OMN-16991). bash keeps exactly one EXIT trap
# per shell, so a later `trap prepush_lock_release EXIT` installed by
# guard_full_suite_host would silently REPLACE this temp-file cleanup and leak
# three mktemp files on every heavy run that took the host slot. Both jobs live
# in one handler instead, so neither can displace the other.
prepush_hook_cleanup() {
  rm -f "${CHANGED_FILE:-}" "${SELECTION_FILE:-}" "${SELECTION_ERR:-}" 2> /dev/null || true
  prepush_lock_release
}
trap prepush_hook_cleanup EXIT

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

# The integration-path addendum the remote leg appends to a heavy escalation's
# argv (`prepush_remote_argv` in prepush_dispatch.sh). It is EMPTY in this repo,
# declared rather than omitted, for two independent reasons:
#
#   1. bash 3.2 -- macOS's system bash, which runs this hook on every lab Mac --
#      raises "unbound variable" for `${#NAME[@]}` on a NEVER-DECLARED array
#      under `set -u`. Newer bash quietly answers 0. prepush_dispatch.sh is a
#      byte-identical copy of omnibase_infra's and therefore reads this name
#      unconditionally, so leaving it undeclared would abort the remote leg on
#      exactly the hosts this port exists to reach.
#   2. It is genuinely empty HERE, not merely unset. OMN-16825's invariant is
#      that an escalation must never run FEWER of the impacted tests than the
#      narrowing it replaces. omnibase_infra needs an addendum because its
#      escalation target is `tests/unit/`, which excludes `tests/integration/
#      chains/`. This repo's target is `tests/` -- the whole tree -- so it is
#      already a superset of every selectable path, and both the local site and
#      the remote wrapper append the same `--ignore=tests/integration`. There is
#      no path the escalation could drop.
RUNNABLE_INTEGRATION_PATHS=()

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

# =============================================================================
# PATH parity with the remote leg (OMN-17549)
# =============================================================================
# Every pytest invocation below this line runs LOCALLY -- on the pusher's own
# machine, or on a lab host taking the OMN-17280 same-host route. The remote
# leg has restored a developer-shell PATH before running the transplanted suite
# since OMN-16989; this leg never did, so the same tree returned a different
# verdict depending on which leg ran it.
#
# Measured 2026-09-02 (OMN-17549): a governed same-host push of THIS repo on
# `.201` returned six reds in tests/scripts/test_shell_hygiene_gate.py because
# the non-interactive PATH there omits `~/.local/bin`, which is where that
# host's `shellcheck` lives. The tool was installed the whole time. Six
# guaranteed false reds hard-block a push, so this is part of the verdict
# meaning anything -- not a convenience.
#
# Set here, once, AFTER every placement decision and BEFORE any pytest run, so
# it covers the fail-closed escalation and the impacted-subset run alike. The
# list is single-sourced with the remote wrapper in prepush_dispatch.sh
# (prepush_developer_shell_path) and the caller's own `${PATH}` stays last, so
# this can only add resolution.
# OMN-17704: refuse loudly rather than assign an empty PATH. A bare
# `PATH="$(prepush_developer_shell_path)"` is silent if the helper is ever
# undefined (dispatch failed to source, or a later refactor renamed it): the
# substitution yields "", PATH becomes the empty string mid-hook AFTER the
# placement decision, and every later lookup fails for a reason that has
# nothing to do with the tree under test. This is a fail-closed surface, so
# silence is the wrong failure mode.
if ! type prepush_developer_shell_path > /dev/null 2>&1; then
  die "prepush_developer_shell_path is not defined -- prepush_dispatch.sh did not source, so the governed suite would run on an unrestored PATH" \
    "This is a hook-integrity failure, not a test failure. Repair the dispatch source; do not work around it by unsetting the check."
fi
_prepush_devpath="$(prepush_developer_shell_path)"
if [ -z "$_prepush_devpath" ]; then
  die "prepush_developer_shell_path returned an empty PATH" \
    "Assigning it would blank PATH for every subsequent command in this hook. Repair the helper; do not bypass."
fi
PATH="$_prepush_devpath"
export PATH
unset _prepush_devpath

if [ "$IS_FULL" = "True" ] || [ "$IS_FULL" = "true" ]; then
  guard_full_suite_host
  if [ "$REMOTE_LAB_RUN_VERDICT" -eq 1 ]; then
    # A designated lab host already ran THIS EXACT TREE green over the remote
    # leg (OMN-16991), bound to this sha by a completion marker carrying
    # {head_sha, argv_sha, exit, collected, log_sha256}. Re-running the same
    # suite locally would burn hours to re-derive an answer we hold, on the
    # host the guard just measured as unfit. Skipping it is not a discount on
    # the gate: the verdict came from a real pytest exit code on hardware this
    # repo designates, and a remote RED never reaches here --
    # dispatch_to_lab_host refuses the push itself in that case.
    log "SKIPPING the local full suite: it already ran GREEN on a designated lab host for this exact tree."
  else
    log "running FULL suite (fail-closed escalation): uv run pytest ${FULL_SUITE_TARGET} --ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
    (
      _pytest_extra_args="${PREPUSH_PYTEST_ARGS:-}"
      scrub_prepush_override_env
      # shellcheck disable=SC2086
      exec uv run pytest "${FULL_SUITE_TARGET}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${_pytest_extra_args}
    ) || RC=$?
  fi
elif [ "${#PATHS[@]}" -gt 0 ]; then
  # OMN-15408: guard on the SELECTED WORK, not the is_full_suite flag. A
  # selection that covers the whole full-suite target is the heavy run under
  # another name and must be routed to .200 exactly as the flagged escalation is.
  if selection_is_whole_suite "$FULL_SUITE_TARGET" "${PATHS[@]}"; then
    guard_full_suite_host "whole-suite-equivalent impacted selection (is_full_suite=${IS_FULL}, selected paths [ ${PATHS_STR}] cover the entire '${FULL_SUITE_TARGET}' escalation target)"
  fi
  if [ "$REMOTE_LAB_RUN_VERDICT" -eq 1 ]; then
    # Same reasoning as the flagged-escalation site above. Reached only when
    # the selection was whole-suite-equivalent, so guard_full_suite_host ran
    # and a designated lab host already produced a green verdict for this tree
    # over the IDENTICAL argv (prepush_remote_argv ships ${PATHS[@]} on this
    # branch, not the escalation target).
    log "SKIPPING the local impacted run: the whole-suite-equivalent selection already ran GREEN on a designated lab host for this exact tree."
  else
    log "running impacted subset: uv run pytest ${PATHS_STR}--ignore=tests/integration -m '${LOCAL_MARKER_FILTER}' ${PREPUSH_PYTEST_ARGS:-}"
    (
      _pytest_extra_args="${PREPUSH_PYTEST_ARGS:-}"
      scrub_prepush_override_env
      # shellcheck disable=SC2086
      exec uv run pytest "${PATHS[@]}" --ignore=tests/integration -m "${LOCAL_MARKER_FILTER}" --tb=short ${_pytest_extra_args}
    ) || RC=$?
  fi
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
