# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_conflict_hunk_effect [OMN-8991].

SCAFFOLD ONLY — real implementation in OMN-8992.

File safety rules (enforced in OMN-8992 implementation):
  - Only files in an explicit allowlist may be written.
  - pyproject.toml, uv.lock, and other config roots are never writable.
  - Patch must touch ≤50 changed lines; raises ValueError otherwise.
  - Post-mutation scope check: abort + revert if unexpected file touched.
  - LLM output that is syntactically invalid Python → fail-loud, no write.
  - Worktree cleaned on pytest failure after write.

Routing: primary=deepseek-r1-14b, fallback=deepseek-r1-32b (conflict_resolver role),
ci_override.primary=qwen3-coder-30b. All routing via contract.yaml model_routing block.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_hunk_result import (
    ModelConflictHunkResult,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelConflictHunkCommand,
)

_log = logging.getLogger(__name__)

_MAX_CHANGED_LINES = 50

# Files that may never be written by this handler.
_WRITE_BLOCKLIST: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        ".github",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)


class HandlerConflictHunk:
    """Scaffold stub — resolves merge-conflict hunks via LLM patch generation.

    Phase 1 (OMN-8991): raises NotImplementedError on all invocations.
    Phase 2 (OMN-8992): full LLM call + git write-back + commit logic.
    """

    def resolve(self, cmd: ModelConflictHunkCommand) -> ModelConflictHunkResult:
        _log.info(
            "node_conflict_hunk_effect: resolve called for pr=%s repo=%s files=%s",
            cmd.pr_number,
            cmd.repo,
            cmd.conflict_files,
        )
        raise NotImplementedError(  # stub-ok: Phase 1 scaffold; real impl in OMN-8992
            "node_conflict_hunk_effect is a Phase 1 scaffold. "
            "Real implementation ships in OMN-8992."
        )

    @staticmethod
    def _is_blocked_file(filename: str) -> bool:
        """Return True if filename must never be written."""
        return any(
            filename == b or filename.startswith(b + "/") for b in _WRITE_BLOCKLIST
        )

    @staticmethod
    def _validate_patch_size(changed_lines: int) -> None:
        """Raise ValueError if patch exceeds the 50-line safety limit."""
        if changed_lines > _MAX_CHANGED_LINES:
            raise ValueError(
                f"Patch exceeds {_MAX_CHANGED_LINES}-line limit: {changed_lines} lines changed. "
                "Abort to prevent unbounded LLM rewrites."
            )
