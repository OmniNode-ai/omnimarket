# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shape every conformance suite shares: named checks run against a fresh harness each."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict


class ConformanceFailureError(AssertionError):
    """A check found the binding breaking a rule of its protocol."""


class ModelCheckResult(BaseModel):
    """The outcome of one named check against one binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    detail: str = ""


type Check[HarnessT] = tuple[str, Callable[[HarnessT], Awaitable[None]]]


def fresh[HarnessT](factory: Callable[[], HarnessT]) -> Callable[[str], HarnessT]:
    """Adapt a zero-argument binding factory to the per-check form ``run_checks`` takes."""
    return lambda _check_name: factory()


async def run_checks[HarnessT](
    checks: tuple[Check[HarnessT], ...],
    make_harness: Callable[[str], HarnessT],
) -> tuple[ModelCheckResult, ...]:
    """Run each check against a harness built fresh for it, given the check's name.

    The name lets a recorded transcript replay the session recorded for that check. A failure
    is a result, not a raise.
    """
    results: list[ModelCheckResult] = []
    for name, check in checks:
        try:
            await check(make_harness(name))
        except ConformanceFailureError as failure:
            results.append(
                ModelCheckResult(name=name, passed=False, detail=str(failure))
            )
        else:
            results.append(ModelCheckResult(name=name, passed=True))
    return tuple(results)


def expect(condition: bool, detail: str) -> None:
    """Raise a conformance failure naming ``detail`` unless ``condition`` holds."""
    if not condition:
        raise ConformanceFailureError(detail)
