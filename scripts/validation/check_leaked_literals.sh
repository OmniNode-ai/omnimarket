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
#   staged     — scan only files staged in the INDEX (the pre-commit surface).
#
# Usage:
#   bash scripts/validation/check_leaked_literals.sh                    # blocking + all (default)
#   bash scripts/validation/check_leaked_literals.sh advisory all       # advisory + full tree
#   bash scripts/validation/check_leaked_literals.sh advisory diff      # advisory + branch diff
#   bash scripts/validation/check_leaked_literals.sh blocking diff      # blocking + branch diff
#   bash scripts/validation/check_leaked_literals.sh blocking staged    # blocking + staged (pre-commit)
#
# OMN-17369: why `staged` exists and why pre-commit MUST use it.
#   `diff` enumerates `${BASE_REF}...HEAD` — files that are already COMMITTED.
#   At pre-commit time the file being committed lives in the index, not in HEAD,
#   so `diff` never enumerates it. The hook then reported `files_scanned=54
#   findings=0` and PASSED on a staged file carrying a real forbidden literal:
#   a green result produced by scanning the wrong file set. That was measured,
#   not theorised — for the OMN-17320 exposed-identifier class AND for the five
#   OMN-10580 regex classes, since the defect is in this shared enumeration.
#   CI (`scope=all`) always caught it, so the loss was defence-in-depth, not an
#   open door — but a local gate that silently checks nothing is the exact
#   "PASS because it scanned nothing" failure this whole gate family exists to
#   prevent. `staged` reads the index, so the hook sees what is being committed.
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
#     # onex-allow-exposed-identifier OMN-XXXXX reason="<concrete reason>"
#       ^ handled by the DELEGATED exposed-identifier gate, not by the regex
#         catalog below -- see the OMN-17320 section at the end of this file.
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
# docs/plans/2026-05-05-omnimarket-public-shippable.md, generalized to the
# org-wide Tier-1 topology class under OMN-16156 / W0-GATE G1 — see
# docs/plans/2026-08-17-public-docs-kb-consolidation-plan.md §3/§5c):
#   192.168.x.             (LAN block — any /16, not just the .86 subnet)
#   100.64.0.0/10           (Tailscale CGNAT range)
#   *.tail<id>.ts.net       (Tailscale MagicDNS / tailnet hostname)
#   /Users/jonah            (per-user home path)
#   /home/<user>/           (generic Linux home path)
#   /Volumes/<mount>        (per-machine mount, any name — not just PRO-G40)
#   *.svc.cluster.local     (k8s internal service FQDN)
#   18.209.126.195          (known-real external cluster IP — onex-dev)
#   installed_by:<user>     (operator attribution in committed handshake files)
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
# The topology classes above (LAN/CGNAT/MagicDNS/home-path/Volumes-mount/k8s
# FQDN/external-IP/installed_by) are the org-wide generalization target: G1
# (advisory-only, this rollout) extends the pattern catalog so the same
# script — still resident only in omnimarket, per the plan's explicit G1 vs
# G1-FULL split — can be pointed at any sibling repo's working tree via
# scripts/validation/run_leaked_literals_org_wide.sh. Flipping other repos to
# *blocking* mode with their own CI wiring is G1-FULL (post-beta).
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

if [[ "${SCOPE}" != "all" && "${SCOPE}" != "diff" && "${SCOPE}" != "staged" ]]; then
  echo "ERROR: scope must be 'all', 'diff' or 'staged', got '${SCOPE}'" >&2
  exit 2
fi

# Single combined regex. Uses POSIX ERE so it works with BSD grep (macOS)
# and GNU grep (Linux/CI) — no PCRE escapes (\s, \d, non-greedy) anywhere
# below; use POSIX classes ([[:space:]], [0-9]) instead.
#
# Topology classes (OMN-16156 / W0-GATE G1) come first:
#   192\.168\.[0-9]{1,3}\.                    LAN, any /16 subnet
#   100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}
#                                              Tailscale CGNAT (100.64.0.0/10)
#   \.tail[A-Za-z0-9]+\.ts\.net                MagicDNS / tailnet hostname
#   /home/[A-Za-z0-9_.-]+/                     generic Linux home path
#   /Volumes/[A-Za-z0-9_-]+                    any per-machine mount name
#   \.svc\.cluster\.local                      k8s internal service FQDN
#   18\.209\.126\.195                          known-real external cluster IP
#   installed_by:[[:space:]]*[A-Za-z0-9_.-]+   operator attribution
LEAK_REGEX='192\.168\.[0-9]{1,3}\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|\.tail[A-Za-z0-9]+\.ts\.net|/Users/jonah|/home/[A-Za-z0-9_.-]+/|/Volumes/[A-Za-z0-9_-]+|\.svc\.cluster\.local|18\.209\.126\.195|installed_by:[[:space:]]*[A-Za-z0-9_.-]+|cyankiwi/|Corianas/|mlx-community/(Qwen3-Next|DeepSeek|Qwen3-Embedding-8B|Qwen3\.5)|jonahgabriel|dash\.dev\.omninode\.ai|272493677981|OmniCloudPlatformAdmin|i-0e596e8b557e27785|onreviewbot@gmail\.com|super-secret'

# Allowlist annotation: must include leak-class, ticket, and reason.
# Extended with onex-allow-model-id for private HuggingFace model identifiers.
# Type-specific cross-checking (require annotation type to match leak pattern class)
# is deferred to a follow-up gate hardening pass after initial rollout.
ALLOWLIST_REGEX='# onex-allow-(internal-ip|local-path|test-fixture|raw-env|model-id) OMN-[0-9]+ reason="[^"]+"'

# Per-file exemptions (the gate script and its CI workflow obviously contain the
# pattern catalog and must self-reference; the raw-env audit emits a findings
# CSV whose purpose is to preserve literal evidence for follow-up cleanup).
SELF_EXEMPT_FILES=(
  "scripts/validation/check_leaked_literals.sh"
  "scripts/audit/raw_env_usage_audit.py"
  ".github/workflows/reject-leaked-literals.yml"
  "docs/leaked-literals-governance.md"
  ".leaked-literals-allowlist.yaml"
  # Generated audit reports — contain leaked literals as data, not source defaults.
  "docs/audits/2026-05-05-raw-env-usage.csv"
  "docs/audits/2026-05-05-raw-env-usage.md"
  "docs/audits/2026-05-05-contracts-dir-references.csv"
  # Tracking docs may reference lab addresses as examples or config hints.
  "docs/tracking/delegation-cost-projection-lane.md"
  # OMN-16156: ADR-canary ground-truth corpus embeds real merged ADRs'
  # verbatim text ("the full text of the authoritative ADR (inline)" per its
  # own header) as a benchmark fixture. An illustrative IPv4 example inside
  # one embedded ADR's edge-case table is not a real leak, and editing embedded
  # ADR text to annotate it would corrupt the ground-truth fidelity the file
  # exists to provide.
  "docs/adr-canary/ground_truth_manifest.yaml"
)

# Locate the repo root robustly (tolerates being called from elsewhere).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "${REPO_ROOT}" || { echo "could not cd to ${REPO_ROOT}" >&2; exit 2; }

# Build file list (NUL-delimited so spaces in paths survive).
TMP_FILES="$(mktemp)"
trap 'rm -f "${TMP_FILES}"' EXIT

if [[ "${SCOPE}" == "staged" ]]; then
  # OMN-17369: the pre-commit surface. Enumerate the INDEX, not HEAD.
  # --diff-filter=ACMR drops staged deletions (nothing left on disk to scan)
  # and resolves a rename to its new path. pre-commit stashes unstaged changes
  # for the duration of the hook, so the worktree copy this script greps IS the
  # staged content; outside pre-commit the two can differ, and scanning the
  # worktree is the conservative choice (it is what would be committed next).
  git diff --cached --name-only -z --diff-filter=ACMR > "${TMP_FILES}"
fi

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
  [[ "${path}" == docs/audits/*-raw-env-usage.csv ]] && return 0
  # OMN-13294: durable generation-evidence JSON records the live endpoint (a LAN
  # IP) verbatim as the routing-authority proof requires. JSON cannot carry a
  # `# onex-allow-file` comment (escaped quotes break the marker regex), so this
  # evidence-file class is path-exempt — same precedent as the raw-env-usage CSVs.
  [[ "${path}" == docs/evidence/*/*.generation.json ]] && return 0
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

# OMN-17320: delegated exposed-identifier gate.
#
# Why this is delegated rather than folded into LEAK_REGEX above: that catalog
# matches patterns written out in PLAINTEXT. The exposed-identifier class cannot
# use that mechanism -- writing a forbidden customer identifier into a pattern
# list in a PUBLIC repo would create exactly the fresh, greppable, current-tree
# occurrence the class exists to prevent, and would force this file to be exempt
# from its own rule. The delegated gate keys on salted SHA-256 digests instead,
# so nothing here or in its denylist restates a forbidden value.
#
# Why it is wired HERE rather than as its own CI job: "Leaked Literals Gate" is
# already in omnimarket dev's required_status_checks AND already a pre-commit
# hook, so delegating makes the new class blocking on both surfaces the moment
# this lands -- no branch-protection edit, and no new workflow to classify
# against tests/unit/scripts/ci/test_ci_summary_gate.py's completeness test.
#
# Provenance: OMN-17288 scrubbed a live tenant slug + UUID from this repo, and
# omnimarket#2239 reintroduced the slug three hours later with every gate green.
# Resolved against THIS script's own directory, not the caller's CWD: the gate is
# routinely invoked with a different working directory (pre-commit from the repo
# root, CI from the checkout, and the validation tests from a throwaway fixture
# repo under tmp_path). A CWD-relative path made the sibling look "missing" in
# exactly those cases and tripped the fail-closed branch below, turning a healthy
# gate into a hard exit 2. The sibling ships beside this file, so its own
# directory is the only correct anchor.
EXPOSED_ID_GATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_exposed_identifiers.py"
exposed_id_status=0
if [[ -f "${EXPOSED_ID_GATE}" ]]; then
  # Reuse this run's mode and scope so local (diff) and CI (all) stay aligned,
  # and pass through the SAME base ref this script resolved -- the two gates
  # must never disagree about what "the diff" is.
  PY_BIN="${PYTHON:-python3}"
  if [[ "${SCOPE}" == "staged" ]]; then
    # OMN-17369: hand the staged paths over explicitly rather than teaching the
    # Python scanner a `staged` scope of its own. That file is byte-identical
    # across omnimarket and omnibase_infra (OMN-17320 AC7, pinned by a cross-repo
    # fingerprint test), so forking it here to fix a caller-side enumeration bug
    # would break parity in both repos to fix one. The scanner already accepts
    # explicit paths; this reuses that surface.
    staged_paths=()
    while IFS= read -r -d '' staged_file; do
      # Skip anything gone from disk: the scanner treats a missing path as a
      # config error (exit 2), and a race here must not turn into a hard stop.
      [[ -f "${staged_file}" ]] && staged_paths+=("${staged_file}")
    done < "${TMP_FILES}"
    # An empty argv would make the scanner fall back to its own scope=all and
    # walk the whole tree — slow, and not what "nothing staged" means.
    if (( ${#staged_paths[@]} > 0 )); then
      "${PY_BIN}" "${EXPOSED_ID_GATE}" --mode "${MODE}" -- "${staged_paths[@]}" \
        || exposed_id_status=$?
    fi
  elif [[ "${SCOPE}" == "diff" ]]; then
    "${PY_BIN}" "${EXPOSED_ID_GATE}" --mode "${MODE}" --scope diff \
      --base-ref "${BASE_REF:-origin/main}" || exposed_id_status=$?
  else
    "${PY_BIN}" "${EXPOSED_ID_GATE}" --mode "${MODE}" --scope all || exposed_id_status=$?
  fi
else
  # Fail closed: a missing delegated gate is a silently-unenforced class.
  echo "leak-gate: ERROR ${EXPOSED_ID_GATE} is missing (OMN-17320)" >&2
  exposed_id_status=2
fi

# blocking mode — fail on any finding.
if (( ${#findings[@]} > 0 )); then
  echo "leak-gate: blocking — ${#findings[@]} unallowlisted findings"
  exit 1
fi

if (( exposed_id_status != 0 )); then
  echo "leak-gate: blocking — delegated exposed-identifier gate failed (exit ${exposed_id_status})"
  exit "${exposed_id_status}"
fi

exit 0
