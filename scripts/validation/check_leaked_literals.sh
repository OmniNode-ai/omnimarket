#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# OMN-10580 (hardens OMN-10554): leaked-literals gate — BLOCKING MODE.
#
# Scans the omnimarket tree for personal/home-lab/AWS-account literals that
# must not ship in a public package. Mode is controlled by the first arg:
#
#   blocking   — print findings; exit 1 on any unannotated hit (default).
#   advisory   — print findings; ALWAYS exit 0 (use for auditing only).
#
# Optional second arg controls scope:
#
#   all        — scan the full tree (default).
#   diff       — scan only files modified in the current branch vs origin/main.
#
# Usage:
#   bash scripts/validation/check_leaked_literals.sh                    # blocking + all (default)
#   bash scripts/validation/check_leaked_literals.sh advisory all       # advisory + full tree
#   bash scripts/validation/check_leaked_literals.sh advisory diff      # advisory + branch diff
#   bash scripts/validation/check_leaked_literals.sh blocking diff      # blocking + branch diff
#
# Scope policy (all paths scanned; annotations permitted everywhere):
#   All files except self-exempt gate scripts and ignored dirs are scanned.
#   Any line carrying a valid annotation on the SAME line as the literal is
#   exempt from blocking.
#
#   Valid annotation form (ticket + reason required):
#     # onex-allow-internal-ip OMN-XXXXX reason="<concrete reason>"
#     # onex-allow-local-path OMN-XXXXX reason="<concrete reason>"
#     # onex-allow-test-fixture OMN-XXXXX reason="<concrete reason>"
#     # onex-allow-raw-env OMN-XXXXX reason="<concrete reason>"
#     # onex-allow-model-id OMN-XXXXX reason="<concrete reason>"
#
#   A bare annotation without ticket+reason (e.g. `# onex-allow-internal-ip`)
#   is REJECTED — every exception must be ticketed and reasoned.
#
# File-level exemption (for test fixtures with many deliberate occurrences):
#   Add anywhere in the file:
#     # onex-allow-file OMN-XXXXX reason="<concrete reason>"
#   The entire file is skipped. Use sparingly; prefer per-line annotations in
#   production source. Test fixture files that test the gate itself are the
#   primary intended use.
#
#   Ignored dirs: .git/**, dist/**, build/**, .venv/**, node_modules/**, *.lock
#
# Patterns scanned (mirrors the leak-class catalog in
# docs/plans/2026-05-05-omnimarket-public-shippable.md):
#   192.168.86.            (LAN block)
#   /Users/jonah           (per-user home path)
#   /Volumes/PRO-G40       (per-machine mount)
#   cyankiwi/              (private HF org — coder model)
#   Corianas/              (private HF org — reasoner model)
#   mlx-community/Qwen3-Next | DeepSeek | Qwen3-Embedding-8B | Qwen3.5
#   jonahgabriel           (personal handle)
#   dash.dev.omninode.ai   (private dashboard host)
#   272493677981           (AWS account id)
#   OmniCloudPlatformAdmin (AWS SSO role)
#   i-0e596e8b557e27785    (EC2 instance id)
#   onreviewbot@gmail.com  (personal email)
#   super-secret           (test-fixture credential placeholder that looks credentialed)
#
# Governance: docs/leaked-literals-governance.md
#
# Filenames with spaces are handled (uses NUL-delimited file enumeration).

set -uo pipefail

MODE="${1:-blocking}"
SCOPE="${2:-all}"

if [[ "${MODE}" != "advisory" && "${MODE}" != "blocking" ]]; then
  echo "ERROR: mode must be 'advisory' or 'blocking', got '${MODE}'" >&2
  exit 2
fi

if [[ "${SCOPE}" != "all" && "${SCOPE}" != "diff" ]]; then
  echo "ERROR: scope must be 'all' or 'diff', got '${SCOPE}'" >&2
  exit 2
fi

# Single combined regex. Uses POSIX ERE so it works with BSD grep (macOS)
# and GNU grep (Linux/CI). Each alternative is a leak class.
LEAK_REGEX='192\.168\.86\.|/Users/jonah|/Volumes/PRO-G40|cyankiwi/|Corianas/|mlx-community/(Qwen3-Next|DeepSeek|Qwen3-Embedding-8B|Qwen3\.5)|jonahgabriel|dash\.dev\.omninode\.ai|272493677981|OmniCloudPlatformAdmin|i-0e596e8b557e27785|onreviewbot@gmail\.com|super-secret'

# Allowlist annotation: must include leak-class, ticket, and reason.
# Extended with onex-allow-model-id for private HuggingFace model identifiers.
ALLOWLIST_REGEX='# onex-allow-(internal-ip|local-path|test-fixture|raw-env|model-id) OMN-[0-9]+ reason="[^"]+"'

# Per-file exemptions (the gate script and its CI workflow obviously contain the
# pattern catalog and must self-reference; that is not a leak).
SELF_EXEMPT_FILES=(
  "scripts/validation/check_leaked_literals.sh"
  ".github/workflows/reject-leaked-literals.yml"
  "docs/leaked-literals-governance.md"
  ".leaked-literals-allowlist.yaml"
  # Generated audit reports — contain leaked literals as data, not source defaults.
  "docs/audits/2026-05-05-raw-env-usage.csv"
  "docs/audits/2026-05-05-raw-env-usage.md"
  "docs/audits/2026-05-05-contracts-dir-references.csv"
  # Tracking docs may reference lab addresses as examples or config hints.
  "docs/tracking/delegation-cost-projection-lane.md"
)

# Locate the repo root robustly (tolerates being called from elsewhere).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "${REPO_ROOT}" || { echo "could not cd to ${REPO_ROOT}" >&2; exit 2; }

# Build file list (NUL-delimited so spaces in paths survive).
TMP_FILES="$(mktemp)"
trap 'rm -f "${TMP_FILES}"' EXIT

if [[ "${SCOPE}" == "diff" ]]; then
  BASE_REF="${BASE_REF:-origin/main}"
  if ! git rev-parse --verify "${BASE_REF}" >/dev/null 2>&1; then
    echo "WARN: ${BASE_REF} not found; falling back to scope=all" >&2
    SCOPE="all"
  else
    git diff --name-only -z "${BASE_REF}"...HEAD > "${TMP_FILES}"
  fi
fi

if [[ "${SCOPE}" == "all" ]]; then
  # All tracked + new files, NUL-delimited; exclude common ignore dirs.
  git ls-files -coz --exclude-standard \
    -- ':!:.git/**' ':!:dist/**' ':!:build/**' ':!:.venv/**' \
       ':!:node_modules/**' ':!:**/*.lock' \
    > "${TMP_FILES}"
fi

# Scan each file. Track findings as `<file>:<line>:<content>`.
findings=()
total_scanned=0

_is_self_exempt() {
  local path="$1"
  for exempt in "${SELF_EXEMPT_FILES[@]}"; do
    [[ "${path}" == "${exempt}" ]] && return 0
  done
  return 1
}

while IFS= read -r -d '' f; do
  [[ -z "${f}" ]] && continue
  # Skip files that no longer exist (deleted in diff mode).
  [[ ! -f "${f}" ]] && continue
  # Skip the gate script itself and its workflow file.
  _is_self_exempt "${f}" && continue
  total_scanned=$((total_scanned + 1))

  # File-level exemption: if the file contains a # onex-allow-file annotation
  # anywhere, skip it entirely. Used for test fixtures that deliberately contain
  # pattern literals as test data. Must include a reason.
  FILE_LEVEL_EXEMPT_REGEX='# onex-allow-file OMN-[0-9]+ reason="[^"]+"'
  if grep -qE "${FILE_LEVEL_EXEMPT_REGEX}" -- "${f}" 2>/dev/null; then
    continue
  fi

  # Pull every line containing a leak literal.
  hits="$(grep -nE "${LEAK_REGEX}" -- "${f}" 2>/dev/null || true)"
  [[ -z "${hits}" ]] && continue

  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue

    # Any path: annotation on the same line as the literal exempts it.
    if grep -qE "${ALLOWLIST_REGEX}" <<<"${line}"; then
      continue
    fi

    findings+=("${f}:${line}")
  done <<<"${hits}"
done < "${TMP_FILES}"

# Report.
echo "leak-gate: mode=${MODE} scope=${SCOPE} files_scanned=${total_scanned} findings=${#findings[@]}"
if (( ${#findings[@]} > 0 )); then
  printf '  %s\n' "${findings[@]}"
fi

if [[ "${MODE}" == "advisory" ]]; then
  echo "leak-gate: advisory mode — exit 0 regardless of findings (add annotations + rerun blocking to enforce)"
  exit 0
fi

# blocking mode — fail on any finding.
if (( ${#findings[@]} > 0 )); then
  echo "leak-gate: blocking — ${#findings[@]} unallowlisted findings"
  exit 1
fi

exit 0
