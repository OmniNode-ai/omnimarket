#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-10552: cross-repo audit for references to omnimarket/contracts/.
#
# When the work-tracking YAMLs move from omnimarket/contracts/ to
# omnimarket/docs/work-tracking/contracts/, every external reference must be
# updated or annotated. This script walks every repo under $OMNI_HOME and
# emits a CSV of references found, so the move PR has a closed audit trail.
#
# Usage:
#   bash scripts/audit/contracts_dir_references_audit.sh [--out PATH]
#
# Default output: docs/audits/2026-05-05-contracts-dir-references.csv
# (relative to omnimarket repo root).

set -uo pipefail

# Resolve OMNI_HOME — we walk every sibling repo to find references.
if [[ -z "${OMNI_HOME:-}" ]]; then
  echo "ERROR: OMNI_HOME must be set (e.g. export OMNI_HOME=/Users/<you>/Code/omni_home)" >&2
  exit 2
fi

if [[ ! -d "${OMNI_HOME}" ]]; then
  echo "ERROR: \$OMNI_HOME does not exist: ${OMNI_HOME}" >&2
  exit 2
fi

OUT_PATH="${1:-}"
if [[ "${OUT_PATH}" == "--out" ]]; then
  OUT_PATH="${2:-}"
fi
if [[ -z "${OUT_PATH}" ]]; then
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  OUT_PATH="${REPO_ROOT}/docs/audits/2026-05-05-contracts-dir-references.csv"
fi

mkdir -p "$(dirname "${OUT_PATH}")"

# Reference patterns. We grep for either the literal path
# `omnimarket/contracts/` or the bare `contracts/OMN-` glob — the latter
# catches scripts that cd into omnimarket/ first and reference relative paths.
PATTERN='omnimarket/contracts/|(^|[^a-zA-Z_0-9/])contracts/OMN-[0-9]+'

echo "repo,file,line,context" > "${OUT_PATH}"

shopt -s nullglob
total=0
for repo_dir in "${OMNI_HOME}"/*/; do
  repo="$(basename "${repo_dir%/}")"
  # Skip non-repo dirs (e.g. omni_worktrees, .onex_state).
  [[ -d "${repo_dir}.git" || -d "${repo_dir}.git" ]] || continue

  # Use ripgrep if available (fast, respects .gitignore); fall back to grep -R.
  if command -v rg >/dev/null 2>&1; then
    cmd=(rg --no-heading --line-number -e "${PATTERN}" "${repo_dir}")
  else
    cmd=(grep -rEn "${PATTERN}" "${repo_dir}")
  fi
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    file="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    context="${rest#*:}"
    printf '%s,%s,%s,%s\n' "${repo}" "${file}" "${lineno}" \
      "$(printf '%s' "${context}" | tr ',' ' ' | tr -d '\n' | head -c 240)" \
      >> "${OUT_PATH}"
    total=$((total + 1))
  done < <("${cmd[@]}" 2>/dev/null || true)
done

echo "wrote ${total} reference rows to ${OUT_PATH}"
