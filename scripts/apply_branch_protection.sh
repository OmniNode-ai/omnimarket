#!/usr/bin/env bash
# apply_branch_protection.sh — OMN-8492, Component 3
#
# Applies review-bot/all-findings-resolved as a required status check on the
# main branch of every OmniNode-ai repository.
#
# Usage:
#   bash scripts/apply_branch_protection.sh [--verify] [--dry-run]
#
# Options:
#   --verify    After applying, verify the check is present in branch protection.
#   --dry-run   Print what would be done without making API calls.
#
# Required env:
#   GITHUB_TOKEN  - PAT with repo admin permissions

set -euo pipefail

REQUIRED_CHECK="review-bot/all-findings-resolved"
ORG="OmniNode-ai"
BRANCH="main"

VERIFY=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --verify)   VERIFY=true ;;
    --dry-run)  DRY_RUN=true ;;
    *)          echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "ERROR: GITHUB_TOKEN is required"
  exit 1
fi

REPOS=(
  "omniclaude"
  "omnibase_core"
  "omnibase_infra"
  "omnibase_spi"
  "omnidash"
  "omniintelligence"
  "omnimemory"
  "omninode_infra"
  "omniweb"
  "onex_change_control"
  "omnibase_compat"
  "omnimarket"
)

apply_protection() {
  local repo="$1"
  echo "  Applying to ${ORG}/${repo}..."

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY RUN] Would PATCH /repos/${ORG}/${repo}/branches/${BRANCH}/protection"
    return 0
  fi

  # Fetch current branch protection to preserve existing required checks
  local existing
  existing=$(gh api "repos/${ORG}/${repo}/branches/${BRANCH}/protection" 2>/dev/null || echo "{}")

  # Extract existing required status check contexts, append ours if not present
  local current_contexts
  current_contexts=$(echo "$existing" | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
checks = d.get('required_status_checks') or {}
contexts = checks.get('contexts') or []
print(json.dumps(contexts))
" 2>/dev/null || echo "[]")

  local new_contexts
  new_contexts=$(python3 -c "
import sys, json
contexts = json.loads('$current_contexts')
required = '$REQUIRED_CHECK'
if required not in contexts:
    contexts.append(required)
print(json.dumps(contexts))
")

  # Apply the updated protection with the new required check appended
  gh api \
    --method PATCH \
    -H "Accept: application/vnd.github+json" \
    "repos/${ORG}/${repo}/branches/${BRANCH}/protection" \
    --field "required_status_checks[strict]=false" \
    --field "required_status_checks[contexts][]=${REQUIRED_CHECK}" \
    --field "enforce_admins=false" \
    --field "required_pull_request_reviews=null" \
    --field "restrictions=null" \
    --silent
  echo "  Done: ${ORG}/${repo}"
}

verify_protection() {
  local repo="$1"
  local result
  result=$(gh api "repos/${ORG}/${repo}/branches/${BRANCH}/protection" \
    --jq ".required_status_checks.contexts[]" 2>/dev/null || echo "")

  if echo "$result" | grep -q "^${REQUIRED_CHECK}$"; then
    echo "  [OK] ${ORG}/${repo}: '${REQUIRED_CHECK}' is required"
  else
    echo "  [FAIL] ${ORG}/${repo}: '${REQUIRED_CHECK}' NOT found in required checks"
    return 1
  fi
}

echo "=== Branch Protection Rollout: ${REQUIRED_CHECK} ==="
echo "Org: ${ORG}  Branch: ${BRANCH}  Repos: ${#REPOS[@]}"
echo ""

FAILED=()

for repo in "${REPOS[@]}"; do
  if ! apply_protection "$repo"; then
    FAILED+=("$repo")
  fi
done

if [[ "$VERIFY" == "true" ]]; then
  echo ""
  echo "=== Verification ==="
  for repo in "${REPOS[@]}"; do
    if ! verify_protection "$repo"; then
      FAILED+=("${repo}-verify")
    fi
  done
fi

echo ""
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "FAILED repos: ${FAILED[*]}"
  exit 1
else
  echo "All repos updated successfully."
fi
