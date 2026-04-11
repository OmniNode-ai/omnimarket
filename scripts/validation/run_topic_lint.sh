#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
#
# Run topic-naming-lint against omnimarket contract YAML files and Python source.
# Mirrors the omnibase_infra pattern. (OMN-8507)
#
# Usage: invoked by pre-commit as a system-language hook, or directly:
#   bash scripts/validation/run_topic_lint.sh
set -euo pipefail

OMNIBASE_INFRA="${OMNIBASE_INFRA_PATH:-../omnibase_infra}"
LINT="$OMNIBASE_INFRA/scripts/validation/lint_topic_names.py"
BASELINE="$(dirname "${BASH_SOURCE[0]}")/topic_naming_baseline.txt"
RC=0

if [ ! -f "$LINT" ]; then
  echo "ERROR: lint_topic_names.py not found at $LINT" >&2
  echo "Set OMNIBASE_INFRA_PATH to the omnibase_infra repo root." >&2
  exit 2
fi

BASELINE_ARG=""
if [ -f "$BASELINE" ]; then
  BASELINE_ARG="--baseline $BASELINE"
fi

# Resolve a Python interpreter that has PyYAML available.
# Priority: sibling omnibase_infra .venv (has yaml) → $OMNI_HOME venv → uv run
_py_has_yaml() { "$1" -c "import yaml" 2>/dev/null; }
RUN_PYTHON=""
if [ -f "$OMNIBASE_INFRA/.venv/bin/python" ] && _py_has_yaml "$OMNIBASE_INFRA/.venv/bin/python"; then
  RUN_PYTHON="$OMNIBASE_INFRA/.venv/bin/python"
elif [ -n "${OMNI_HOME:-}" ] && [ -f "$OMNI_HOME/omnimarket/.venv/bin/python" ] && _py_has_yaml "$OMNI_HOME/omnimarket/.venv/bin/python"; then
  RUN_PYTHON="$OMNI_HOME/omnimarket/.venv/bin/python"
fi

if [ -n "$RUN_PYTHON" ]; then
  "$RUN_PYTHON" "$LINT" --scan-contracts src/omnimarket/nodes $BASELINE_ARG || RC=$?
  "$RUN_PYTHON" "$LINT" --scan-python src/omnimarket $BASELINE_ARG || RC=$?
else
  uv run python "$LINT" --scan-contracts src/omnimarket/nodes $BASELINE_ARG || RC=$?
  uv run python "$LINT" --scan-python src/omnimarket $BASELINE_ARG || RC=$?
fi

exit "$RC"
