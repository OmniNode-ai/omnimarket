#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-16156 (W0-GATE / G1, advisory-only beta-now rollout of the
# omnimarket-only leaked-literals gate — see
# docs/plans/2026-08-17-public-docs-kb-consolidation-plan.md §5c).
#
# check_leaked_literals.sh resolves its own scan root via
# `git rev-parse --show-toplevel`, so it is already repo-root-agnostic: it
# does not need to be copied into another repo to scan that repo's tree. This
# driver runs it, in ADVISORY mode only, against every sibling repo clone
# under $OMNI_HOME, reporting findings without failing anything.
#
# G1 explicitly does NOT (that is G1-FULL, post-beta, see §5c):
#   - package/distribute the gate script itself into 15 repos as one shared
#     source (it stays resident in omnimarket; this driver invokes it via a
#     relative path against a different cwd)
#   - wire this driver into any repo's CI or pre-commit (no required check
#     changes anywhere)
#   - annotate or clean up any finding this run surfaces in another repo
#
# Usage:
#   OMNI_HOME=/path/to/omni_home bash scripts/validation/run_leaked_literals_org_wide.sh
#   # or, if OMNI_HOME is already exported (the standard convention):
#   bash scripts/validation/run_leaked_literals_org_wide.sh
#
# Exit code is always 0 (advisory) unless the driver itself hits a usage
# error (missing OMNI_HOME, gate script not found) — a per-repo scan failure
# never aborts the remaining repos.

set -uo pipefail

: "${OMNI_HOME:?OMNI_HOME must be set to the omni_home root (no silent default — see omni_home/CLAUDE.md rule 8)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE_SCRIPT="${SCRIPT_DIR}/check_leaked_literals.sh"

if [[ ! -f "${GATE_SCRIPT}" ]]; then
  echo "ERROR: gate script not found at ${GATE_SCRIPT}" >&2
  exit 2
fi

# Plan §5c / task scope: every repo listed against this item.
ORG_REPOS=(
  omnimarket
  omnibase_core
  omnibase_infra
  omnibase_spi
  omnibase_compat
  omniclaude
  omnimemory
  omniintelligence
  onex_change_control
  omnidash
  omnicursor
  omnigemini
  knowledge-base
  omnibase
)

total_repos_scanned=0
total_repos_skipped=0
total_findings=0

echo "leak-gate-org-wide: driver=OMN-16156 mode=advisory repos_targeted=${#ORG_REPOS[@]}"

for repo in "${ORG_REPOS[@]}"; do
  repo_path="${OMNI_HOME}/${repo}"
  if [[ ! -d "${repo_path}/.git" ]]; then
    echo "leak-gate-org-wide: SKIP ${repo} (no local clone at ${repo_path})"
    total_repos_skipped=$((total_repos_skipped + 1))
    continue
  fi

  echo "--- ${repo} ---"
  # Run in a subshell so a `cd` failure or the gate's own `trap`/`cd` never
  # leaks into this driver's working directory or aborts the loop.
  output="$(cd "${repo_path}" && bash "${GATE_SCRIPT}" advisory all 2>&1)"
  echo "${output}"
  total_repos_scanned=$((total_repos_scanned + 1))

  # Advisory mode always exits 0; parse the summary line for the finding
  # count so this driver can print an org-wide total.
  repo_findings="$(printf '%s\n' "${output}" | grep -oE 'findings=[0-9]+' | head -1 | grep -oE '[0-9]+' || echo 0)"
  total_findings=$((total_findings + repo_findings))
done

echo "leak-gate-org-wide: repos_scanned=${total_repos_scanned} repos_skipped=${total_repos_skipped} total_findings=${total_findings}"
echo "leak-gate-org-wide: advisory mode — exit 0 regardless of findings (G1-FULL flips individual repos to blocking, post-beta)"
exit 0
