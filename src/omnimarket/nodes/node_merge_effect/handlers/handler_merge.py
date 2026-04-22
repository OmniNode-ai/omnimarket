"""
Handler for Merge Effect node.

Attempts git merge origin/main for conflict-free or trivially-mergeable PRs.
Escalates to LLM-based conflict resolution when merge is ambiguous.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_merge_effect.models.model_merge_command import (
    ModelMergeCommand,
)


class HandlerMergeEffect:
    """Handler that performs simple git merge operations."""

    async def initialize(self) -> None:
        """Initialize handler - verify git is available."""
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("Git is not available")

    async def handle(self, data: ModelMergeCommand) -> dict[str, Any]:
        """
        Attempt git merge for the given PR.

        Returns:
            merged: Whether merge succeeded
            conflicts_resolved: Whether conflicts were auto-resolved
            requires_llm: Whether LLM-based resolution is needed
            error: Error message if merge failed
        """
        repo_path = Path(data.repo_path)
        branch = data.branch
        base_branch = data.base_branch or "origin/main"

        if not repo_path.exists():
            return {
                "merged": False,
                "conflicts_resolved": False,
                "requires_llm": False,
                "error": f"Repository path does not exist: {repo_path}",
            }

        try:
            result = await self._attempt_merge(
                repo_path, branch, base_branch, data.dry_run
            )
            return result
        except Exception as exc:
            return {
                "merged": False,
                "conflicts_resolved": False,
                "requires_llm": False,
                "error": str(exc),
            }

    async def _attempt_merge(
        self, repo_path: Path, branch: str, base_branch: str, dry_run: bool
    ) -> dict[str, Any]:
        """Attempt the git merge operation."""
        git_commands = [
            ["git", "fetch", "origin"],
            ["git", "checkout", branch],
            ["git", "merge", "--no-edit", base_branch]
            if not dry_run
            else ["git", "merge", "--no-commit", "--no-ff", base_branch],
        ]

        for cmd in git_commands:
            try:
                result = subprocess.run(
                    cmd,
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                return {
                    "merged": False,
                    "conflicts_resolved": False,
                    "requires_llm": False,
                    "error": f"Git command timed out: {' '.join(cmd)}",
                }

            if result.returncode != 0:
                if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
                    return await self._handle_conflict(repo_path, branch, base_branch)
                return {
                    "merged": False,
                    "conflicts_resolved": False,
                    "requires_llm": False,
                    "error": f"Git command failed: {' '.join(cmd)}\n{result.stderr}",
                }

        if dry_run:
            abort_result = subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if abort_result.returncode != 0:
                return {
                    "merged": False,
                    "conflicts_resolved": False,
                    "requires_llm": False,
                    "error": f"Dry-run cleanup failed: {abort_result.stderr}",
                }

        return {
            "merged": True,
            "conflicts_resolved": True,
            "requires_llm": False,
            "error": "",
        }

    async def _handle_conflict(
        self, repo_path: Path, branch: str, base_branch: str
    ) -> dict[str, Any]:
        """Handle merge conflict - check if simple or needs LLM."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {
                "merged": False,
                "conflicts_resolved": False,
                "requires_llm": False,
                "error": "Git diff timed out during conflict detection",
            }

        conflicting_files = (
            result.stdout.strip().split("\n") if result.stdout.strip() else []
        )

        abort_result = subprocess.run(
            ["git", "merge", "--abort"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if abort_result.returncode != 0:
            return {
                "merged": False,
                "conflicts_resolved": False,
                "requires_llm": False,
                "error": f"Failed to abort merge after conflict: {abort_result.stderr}",
            }

        if not conflicting_files:
            return {
                "merged": False,
                "conflicts_resolved": False,
                "requires_llm": False,
                "error": "Merge conflict detected but no conflicting files found",
            }

        return {
            "merged": False,
            "conflicts_resolved": False,
            "requires_llm": True,
            "error": f"Conflicts in {len(conflicting_files)} file(s): {', '.join(conflicting_files[:5])}",
        }
