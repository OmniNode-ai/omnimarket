#!/usr/bin/env bash
set -euo pipefail

TICKET="OMN-9530"
if [[ "${1:-}" == "--dry-run-only" ]]; then
  shift
elif [[ "${1:-}" != "" ]]; then
  TICKET="$1"
  shift
fi

NODE="omnimarket.nodes.node_ticket_pipeline"

echo "=== TEXT ==="
uv run python -m "$NODE" "$TICKET" --dry-run --output text

echo "=== JSON ==="
uv run python -m "$NODE" "$TICKET" --dry-run --output json

echo "=== YAML ==="
uv run python -m "$NODE" "$TICKET" --dry-run --output yaml

echo "=== MARKDOWN ==="
uv run python -m "$NODE" "$TICKET" --dry-run --output markdown
