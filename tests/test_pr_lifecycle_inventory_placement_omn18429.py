# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18429: inventory placement — the same calls, not more of them.

Measured on the dogfood pass that produced this ticket (run_id
``20260916-090453-b82ab4``): inventory took **5m34s** to enumerate 56-57
org-wide open pull requests, 05:04:53Z to 05:10:28Z. Almost all of it was
waiting. Two causes, both pinned here.

**Serial fan-out.** Every code-host read is its own blocking subprocess, and
they were issued one pull request at a time: four calls per pull request for the
baseline state, plus two more per failed check. On 56 pull requests that is 280
sequential round-trips before any failed-check work. The collection body is
unchanged and is still the only one — what changed is how many of the SAME calls
are in flight at once.

**A census nothing read.** The org-wide open-pull-request census (OMN-13318) ran
once inside every repo's inventory AND once more at the pass level. Only the
pass-level result was ever read. On a twelve-repo sweep that is twelve redundant
paginated org-wide searches per pass.

These cases use a stubbed collection body and a clock, so they prove placement
without touching the network.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    HandlerPrLifecycleInventory,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelPrInventoryInput,
    ModelPrState,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_DELAY_SECONDS = 0.05
_PR_NUMBERS = tuple(range(1, 17))


class _SpyHandler(HandlerPrLifecycleInventory):
    """The real handler with only the network-touching leaves replaced."""

    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.collected: list[int] = []
        self.census_calls = 0
        self.max_in_flight = 0
        self._in_flight = 0
        self._fail_on = fail_on or set()

    def _collect_pr_state(  # type: ignore[override]
        self,
        repo: str,
        pr_number: int,
        *,
        include_check_execution_history: bool = False,
    ) -> ModelPrState:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(_DELAY_SECONDS)
            if pr_number in self._fail_on:
                msg = f"synthetic failure on {pr_number}"
                raise RuntimeError(msg)
            self.collected.append(pr_number)
            return ModelPrState(
                repo=repo, pr_number=pr_number, title=f"PR {pr_number}", state="open"
            )
        finally:
            self._in_flight -= 1

    def _detect_stuck_queue_prs(self, repo: str, pr_states: Any) -> tuple[Any, ...]:  # type: ignore[override]
        return ()

    def collect_org_wide_open_prs(self) -> Any:  # type: ignore[override]
        self.census_calls += 1
        return None


def _input(**kwargs: Any) -> ModelPrInventoryInput:
    return ModelPrInventoryInput(repo=_REPO, pr_numbers=_PR_NUMBERS, **kwargs)


class TestCollectionIsPlacedNotRewritten:
    def test_the_fetch_runs_concurrently_under_the_declared_bound(self) -> None:
        handler = _SpyHandler()
        started = time.monotonic()
        output = handler.handle(_input(max_parallel_fetches=8))
        elapsed = time.monotonic() - started

        serial_floor = len(_PR_NUMBERS) * _DELAY_SECONDS
        assert elapsed < serial_floor / 2, (
            f"{len(_PR_NUMBERS)} pull requests took {elapsed:.2f}s against a "
            f"serial floor of {serial_floor:.2f}s — the fetch is still serial."
        )
        assert output.total_collected == len(_PR_NUMBERS)

    def test_the_bound_is_respected(self) -> None:
        handler = _SpyHandler()
        handler.handle(_input(max_parallel_fetches=4))
        assert 1 < handler.max_in_flight <= 4, (
            "An unbounded fan-out at the code host is how a sweep manufactures "
            f"the rate-limit responses the breaker then judges. Saw "
            f"{handler.max_in_flight} in flight against a bound of 4."
        )

    def test_a_bound_of_one_is_strictly_sequential(self) -> None:
        """Positive control. Without it the timing case proves only a fast clock."""
        handler = _SpyHandler()
        started = time.monotonic()
        handler.handle(_input(max_parallel_fetches=1))
        elapsed = time.monotonic() - started

        assert handler.max_in_flight == 1
        assert elapsed >= len(_PR_NUMBERS) * _DELAY_SECONDS * 0.8

    def test_results_keep_inventory_order(self) -> None:
        """Input order, so nothing downstream has to know this changed."""
        handler = _SpyHandler()
        output = handler.handle(_input(max_parallel_fetches=8))
        assert [state.pr_number for state in output.pr_states] == list(_PR_NUMBERS)

    def test_one_failure_is_still_that_pull_requests_error_alone(self) -> None:
        handler = _SpyHandler(fail_on={4, 9})
        output = handler.handle(_input(max_parallel_fetches=8))

        assert output.total_collected == len(_PR_NUMBERS) - 2
        assert len(output.collection_errors) == 2
        assert all("synthetic failure" in err for err in output.collection_errors)
        assert [state.pr_number for state in output.pr_states] == [
            n for n in _PR_NUMBERS if n not in {4, 9}
        ]


class TestTheRedundantCensusIsGone:
    def test_the_orchestrators_caller_shape_runs_no_per_repo_census(self) -> None:
        handler = _SpyHandler()
        handler.handle(_input(collect_org_wide_census=False))
        assert handler.census_calls == 0, (
            "The pass-level census (OMN-13318) is the one that is read. Running "
            "it per repo as well re-ran the same paginated org-wide search once "
            "per repo, every pass."
        )

    def test_a_standalone_caller_still_gets_a_census(self) -> None:
        """Positive control: the default is unchanged for callers that read it."""
        handler = _SpyHandler()
        handler.handle(_input())
        assert handler.census_calls == 1
