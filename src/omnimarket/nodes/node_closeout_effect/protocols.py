"""Protocol definitions for closeout effect node dependencies.

Protocol-based DI allows tests to inject mocks without importing infrastructure.

Related:
    - OMN-7580: Migrate node_closeout_effect to omnimarket
"""

from __future__ import annotations

from typing import Protocol


class ProtocolMergeSweeper(Protocol):
    """Protocol for merge-sweep operations.

    Implementors enable auto-merge on ready PRs and return merge count.
    """

    async def sweep(self, dry_run: bool = False) -> int:
        """Run merge sweep across org PRs.

        Args:
            dry_run: Skip actual merge operations.

        Returns:
            Number of PRs merged (0 in dry-run mode).
        """
        ...


class ProtocolQualityGateChecker(Protocol):
    """Protocol for quality gate verification.

    Implementors check CI status, coverage thresholds, and other gates.
    """

    async def check(self, dry_run: bool = False) -> bool:
        """Check whether all quality gates pass.

        Args:
            dry_run: Skip actual checks, return True.

        Returns:
            True if all gates pass.
        """
        ...


__all__: list[str] = ["ProtocolMergeSweeper", "ProtocolQualityGateChecker"]
