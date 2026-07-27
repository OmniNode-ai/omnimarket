#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# check_shell_hygiene.sh — fail-closed shellcheck gate over tracked shell scripts
# (OMN-14761 / F-22). Wired as BOTH a pre-commit hook and a CI job; both invoke
# THIS one script so local and CI enforcement can never drift.
#
# Severity tiers:
#   scripts/lib/**  -> --severity=style  (strict bar for new canonical helper
#                      code — the shared merge-controller library)
#   everything else -> --severity=error  (clean across the whole tracked tree
#                      today, so this ships with ZERO baseline and blocks serious
#                      portability/quoting regressions — the zsh word-splitting
#                      class behind F-22)
#
# Usage:
#   scripts/ci/check_shell_hygiene.sh [FILE...]
#     (no args) scan every tracked shell script (CI mode)
#     FILE...   scan the given files (pre-commit passes changed files)
#
# Fail-closed: exits non-zero when shellcheck is missing OR any file has a
# finding at its tier's severity.
set -euo pipefail

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "check_shell_hygiene: shellcheck is required but not installed (fail-closed)." >&2
  echo "  install: brew install shellcheck  |  https://github.com/koalaman/shellcheck" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

# is_shell_file PATH — true for *.sh or an extensionless file with a shell shebang.
is_shell_file() {
  local f="${1}"
  case "${f}" in
    *.sh) return 0 ;;
  esac
  case "${f##*/}" in
    *.*) return 1 ;; # has a non-.sh extension
  esac
  [ -f "${f}" ] || return 1
  local first=""
  IFS= read -r first <"${f}" || true
  case "${first}" in
    '#!'*sh*) return 0 ;;
  esac
  return 1
}

# collect_targets [FILE...] — emit candidate paths (one per line).
collect_targets() {
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@"
    return 0
  fi
  # CI mode: all tracked *.sh, plus extensionless shell scripts under scripts/
  # (e.g. scripts/merge-proof), which `*.sh` globbing would miss.
  git ls-files '*.sh'
  git ls-files -- scripts | while IFS= read -r f; do
    case "${f##*/}" in
      *.*) continue ;;
    esac
    printf '%s\n' "${f}"
  done
}

severity_for() {
  case "${1}" in
    scripts/lib/*) printf 'style' ;;
    *) printf 'error' ;;
  esac
}

rc=0
checked=0
while IFS= read -r f; do
  [ -n "${f}" ] || continue
  is_shell_file "${f}" || continue
  [ -f "${f}" ] || continue
  sev="$(severity_for "${f}")"
  if ! shellcheck --severity="${sev}" "${f}"; then
    echo "  ^ finding in ${f} (checked at --severity=${sev})" >&2
    rc=1
  fi
  checked=$((checked + 1))
done < <(collect_targets "$@" | sort -u)

if [ "${rc}" -eq 0 ]; then
  echo "check_shell_hygiene: ${checked} shell script(s) clean."
else
  echo "check_shell_hygiene: shellcheck findings above — fix them (fail-closed)." >&2
fi
exit "${rc}"
