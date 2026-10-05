# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the runtime_profiles contract validator with no allowlist.

Mirrors the required CI gate `.github/workflows/validator-runtime-profiles.yml`
exactly: it constructs `ValidatorRuntimeProfiles` with an explicitly empty
allowlist and validates `src/`. The repo allowlist drained to empty
(OMN-12982) and OMN-20558 deleted it; `allowlist=set()` also disables repo and
core allowlist discovery, so ANY violating contract fails. No exception list
remains.

This script exists so the local pre-commit hook and CI run the SAME invocation
(OMN-12955).
"""

from __future__ import annotations

from pathlib import Path

from omnibase_core.validation.validator_runtime_profiles import (
    ValidatorRuntimeProfiles,
)

SRC_ROOT = Path("src")


def main() -> int:
    result = ValidatorRuntimeProfiles(allowlist=set()).validate(SRC_ROOT)
    for issue in result.issues:
        print(
            f"[{issue.severity.value}] {issue.file_path}:{issue.line_number}: {issue.message}"
        )
    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
