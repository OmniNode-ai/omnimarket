# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client-side materialization of a CI checkout result over git (an adapter, not a handler: no contract binds it).

The bytes move over git from the served URL; GitHub is never named.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutResult,
)


class CheckoutMaterializeError(Exception):
    """Raised when a git step fails during materialization."""


class CheckoutNotServedError(CheckoutMaterializeError):
    """Raised when the result status is not served or synced_on_miss."""


class CheckoutShaMismatchError(CheckoutMaterializeError):
    """Raised when the checked-out HEAD does not match the requested sha."""


def materialize(
    result: ModelCiCheckoutResult,
    dest: Path,
    *,
    git_timeout_seconds: float,
    run: Callable[[list[str], Path | None, float], int] | None = None,
) -> None:
    """Check out the repository at result.sha into dest from result.serve_url."""
    if result.status not in (
        EnumCiCheckoutStatus.SERVED,
        EnumCiCheckoutStatus.SYNCED_ON_MISS,
    ):
        raise CheckoutNotServedError(result.status)
    if not result.serve_url:
        raise CheckoutMaterializeError("serve_url is required for materialization")

    dest.mkdir(parents=True, exist_ok=True)
    runner = run or _run_git

    steps: list[tuple[str, list[str]]] = [
        ("init", ["git", "init", "-q", str(dest)]),
        (
            "fetch",
            [
                "git",
                "-C",
                str(dest),
                "fetch",
                "-q",
                "--depth=1",
                result.serve_url,
                result.sha,
            ],
        ),
        (
            "checkout",
            ["git", "-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ),
    ]

    for step_name, argv in steps:
        rc = runner(argv, dest, git_timeout_seconds)
        if rc != 0:
            raise CheckoutMaterializeError(
                f"git step '{step_name}' failed with exit {rc}"
            )

    verify_argv = ["git", "-C", str(dest), "rev-parse", "HEAD"]
    proc = subprocess.run(
        verify_argv,
        capture_output=True,
        text=True,
        timeout=git_timeout_seconds,
    )
    if proc.returncode != 0:
        raise CheckoutMaterializeError("git step 'verify' failed")
    actual_sha = proc.stdout.strip()
    if actual_sha != result.sha:
        raise CheckoutShaMismatchError(f"HEAD is {actual_sha}, expected {result.sha}")


def _run_git(argv: list[str], cwd: Path | None, timeout: float) -> int:
    """Run a git command without a shell, returning the exit code."""
    proc = subprocess.run(argv, cwd=cwd, timeout=timeout)
    return proc.returncode
