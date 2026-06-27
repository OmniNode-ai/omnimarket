# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WS-5 Wave 3 — multi-parameter integration coverage for node_ci_watch.

COMPUTE node (Variant A): the handler is driven in-process. The gh-CLI boundary
(``_fetch_ci_status`` / ``_wait_and_repoll``) and the fixer dispatch
(``_dispatch_fix_worker``) are overridden via a subclass that replays a scripted
sequence of typed ``ModelCiStatusFetch`` snapshots — never subprocess
monkeypatch. Each parametrized case asserts the TYPED ``ModelCiWatchResult``
terminal_status + failed_checks + cycle records.

Negative controls:
  * a failing snapshot with auto_fix off must terminate FAILED and surface the
    failing check names;
  * a gh query error must terminate ERROR (the OMN-12428 fail-loud path), never
    a green PASS.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiStatusFetch,
    ModelCiWatchCommand,
    ModelCiWatchResult,
    ModelFailedCheck,
)

REPO = "OmniNode-ai/omnimarket"


def _green() -> ModelCiStatusFetch:
    return ModelCiStatusFetch(failed_checks=[], failure_summary="", query_error=None)


def _failing(*names: str) -> ModelCiStatusFetch:
    checks = [ModelFailedCheck(name=n, conclusion="failure") for n in names]
    return ModelCiStatusFetch(
        failed_checks=checks,
        failure_summary="boom log excerpt",
        query_error=None,
    )


def _query_error() -> ModelCiStatusFetch:
    return ModelCiStatusFetch(
        failed_checks=[],
        failure_summary="gh pr checks error: transport failure",
        query_error="gh pr checks error: transport failure",
    )


class _StubCiWatch(HandlerCiWatch):
    """Replay scripted CI snapshots; stub the fixer dispatch outcome."""

    def __init__(
        self, fetches: list[ModelCiStatusFetch], *, dispatch_ok: bool = True
    ) -> None:
        self._fetches = list(fetches)
        self._cursor = 0
        self._dispatch_ok = dispatch_ok

    def _next(self) -> ModelCiStatusFetch:
        fetch = self._fetches[self._cursor]
        self._cursor += 1
        return fetch

    def _fetch_ci_status(self, repo: str, pr_number: int) -> ModelCiStatusFetch:
        return self._next()

    def _wait_and_repoll(self, repo: str, pr_number: int) -> ModelCiStatusFetch:
        return self._next()

    def _dispatch_fix_worker(  # type: ignore[override]
        self, *, command, cycle_num, failed_checks, failure_summary
    ) -> tuple[str, str, str]:
        if self._dispatch_ok:
            return ("ci-fix-worker", "delegated:1", "")
        return ("ci-fix-worker", "rejected", "fixer dispatch rejected")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fetches", "kwargs", "dispatch_ok", "expect"),
    [
        # dry_run short-circuits to PASSED with no fetch consumed.
        pytest.param(
            [],
            {"dry_run": True},
            True,
            {"status": EnumCiTerminalStatus.PASSED, "failed": 0, "cycles": 0},
            id="dry-run-passed",
        ),
        # A truly-green snapshot is PASSED.
        pytest.param(
            [_green()],
            {},
            True,
            {"status": EnumCiTerminalStatus.PASSED, "failed": 0, "cycles": 0},
            id="green-passed",
        ),
        # NEGATIVE CONTROL: failing + auto_fix off -> FAILED, names surfaced.
        pytest.param(
            [_failing("pytest", "mypy")],
            {"auto_fix": False},
            True,
            {"status": EnumCiTerminalStatus.FAILED, "failed": 2, "cycles": 0},
            id="failing-no-autofix",
        ),
        # NEGATIVE CONTROL: gh query error -> ERROR (never green).
        pytest.param(
            [_query_error()],
            {},
            True,
            {"status": EnumCiTerminalStatus.ERROR, "failed": 0, "cycles": 0},
            id="query-error",
        ),
        # auto_fix: failing then green after one fix cycle -> FIXED.
        pytest.param(
            [_failing("pytest"), _green()],
            {"auto_fix": True, "max_fix_cycles": 3},
            True,
            {"status": EnumCiTerminalStatus.FIXED, "failed": 0, "cycles": 1},
            id="autofix-fixed",
        ),
        # auto_fix where the fixer dispatch is rejected -> UNFIXABLE.
        pytest.param(
            [_failing("pytest")],
            {"auto_fix": True, "max_fix_cycles": 2},
            False,
            {"status": EnumCiTerminalStatus.UNFIXABLE, "failed": 1, "cycles": 1},
            id="autofix-dispatch-rejected-unfixable",
        ),
    ],
)
async def test_ci_watch_multiparam(
    fetches: list[ModelCiStatusFetch],
    kwargs: dict[str, object],
    dispatch_ok: bool,
    expect: dict[str, object],
) -> None:
    handler = _StubCiWatch(fetches, dispatch_ok=dispatch_ok)
    command = ModelCiWatchCommand(
        pr_number=321,
        repo=REPO,
        correlation_id="cid-ci-watch",
        **kwargs,
    )

    result = handler.handle(command)

    assert isinstance(result, ModelCiWatchResult)
    assert result.pr_number == 321
    assert result.repo == REPO
    assert result.terminal_status == expect["status"]
    assert len(result.failed_checks) == expect["failed"]
    assert len(result.cycles) == expect["cycles"]
    if expect["status"] == EnumCiTerminalStatus.FAILED:
        names = {c.name for c in result.failed_checks}
        assert names == {"pytest", "mypy"}
