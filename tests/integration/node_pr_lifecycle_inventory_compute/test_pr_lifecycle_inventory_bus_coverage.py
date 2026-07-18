# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_pr_lifecycle_inventory_compute,
driven over the canonical in-memory bus.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute). The inventory COMPUTE
handler performs its collection through ``gh`` CLI subprocess calls. Rather than
monkeypatch ``subprocess`` (prohibited), the gh-CLI seams are replaced by a
``_MockInventoryHandler`` subclass that overrides exactly the documented,
test-overridable methods (``_gh_pr_view`` / ``_collect_check_runs`` /
``_collect_reviews`` / ``_detect_stuck_queue_prs`` / ``collect_org_wide_open_prs``).
The REAL pure-compute logic — state-literal mapping, conflict derivation, the
PR-associated ci_passing computation (F3 / OMN-13319) — is exercised unchanged.

The stubbed handler is dispatched through ``LocalRuntimeBusAdapter`` over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelPrInventoryInput`` lands on the declared command topic and the runtime
auto-emits the ``ModelPrInventoryOutput`` onto the declared publish topic
``onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1``.

COMPUTE DoD:
  * every declared ``state`` literal (open / closed / merged) reached;
  * every ci_passing outcome (all-green True, one-failure False, manual-dispatch
    filtered-to-None, no-checks None) and both has_conflicts derivations
    (CONFLICTING mergeable, DIRTY merge_state_status);
  * the collection-error path (a failing PR is recorded, not fatal);
  * the org-wide census fail-closed semantics and every payload-parse branch;
  * a negative control: a PR-associated failing check MUST yield ci_passing
    False (never True), and a failed census MUST report sweep_done False.

Zero network calls: no subprocess runs; the bus is fully in-memory.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    HandlerPrLifecycleInventory,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelOrgWideOpenPrInventory,
    ModelPrCheckRun,
    ModelPrInventoryInput,
    ModelPrInventoryOutput,
    ModelPrReview,
    ModelPrState,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_pr_lifecycle_inventory_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-inventory-start.v1"
_PUBLISH_TOPIC = "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"


class _MockInventoryHandler(HandlerPrLifecycleInventory):
    """Inventory handler with the gh-CLI seams replaced by injected fixtures.

    Only the documented test-overridable I/O methods are stubbed; every
    pure-compute code path in ``_collect_pr_state``/``handle`` runs unchanged.
    """

    def __init__(
        self,
        *,
        pr_views: dict[int, dict[str, Any]],
        check_runs: dict[int, list[ModelPrCheckRun]] | None = None,
        reviews: dict[int, list[ModelPrReview]] | None = None,
        census: ModelOrgWideOpenPrInventory | None = None,
        raise_for: frozenset[int] = frozenset(),
    ) -> None:
        self._pr_views = pr_views
        self._check_runs = check_runs or {}
        self._reviews = reviews or {}
        self._census = census or ModelOrgWideOpenPrInventory(open_count=0)
        self._raise_for = raise_for

    def _gh_pr_view(self, repo: str, pr_number: int) -> dict[str, Any]:
        if pr_number in self._raise_for:
            raise RuntimeError(f"gh pr view failed for #{pr_number}")
        return self._pr_views[pr_number]

    def _collect_check_runs(
        self,
        repo: str,
        pr_number: int,
        *,
        current_head_sha: str | None = None,
    ) -> list[ModelPrCheckRun]:
        return self._check_runs.get(pr_number, [])

    def _collect_reviews(self, repo: str, pr_number: int) -> list[ModelPrReview]:
        return self._reviews.get(pr_number, [])

    def _detect_stuck_queue_prs(
        self, repo: str, pr_states: list[ModelPrState]
    ) -> list[Any]:
        return []

    def collect_org_wide_open_prs(self) -> ModelOrgWideOpenPrInventory:
        return self._census


async def _run_over_bus(
    bus: Any, handler: HandlerPrLifecycleInventory, command: ModelPrInventoryInput
) -> ModelPrInventoryOutput:
    """Publish an inventory command onto the declared command topic and return the
    terminal ``ModelPrInventoryOutput`` parsed off the declared publish topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name="pr-lifecycle-inventory-compute",
        input_model_cls=ModelPrInventoryInput,
        output_topic=_PUBLISH_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-pr-lifecycle-inventory-test",
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=command.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_PUBLISH_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_PUBLISH_TOPIC}"
    return ModelPrInventoryOutput.model_validate(json.loads(history[-1].value))


@pytest.mark.integration
async def test_inventory_collects_all_state_literals_over_bus(
    integration_event_bus: Any,
) -> None:
    """open / closed / merged state literals all round-trip through the terminal event."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _MockInventoryHandler(
            pr_views={
                1: {"title": "open pr", "state": "open", "mergeable": "MERGEABLE"},
                2: {"title": "closed pr", "state": "closed", "mergeable": "MERGEABLE"},
                3: {"title": "merged pr", "state": "merged", "mergeable": "MERGEABLE"},
            },
            census=ModelOrgWideOpenPrInventory(open_count=0),
        )
        output = await _run_over_bus(
            bus,
            handler,
            ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(1, 2, 3)),
        )
        assert output.total_collected == 3
        by_number = {s.pr_number: s for s in output.pr_states}
        assert by_number[1].state == "open"
        assert by_number[2].state == "closed"
        assert by_number[3].state == "merged"
        assert output.collection_errors == ()
    finally:
        await bus.close()


@pytest.mark.integration
async def test_inventory_conflict_derivations_over_bus(
    integration_event_bus: Any,
) -> None:
    """has_conflicts is derived from CONFLICTING mergeable and from DIRTY state."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _MockInventoryHandler(
            pr_views={
                10: {"state": "open", "mergeable": "CONFLICTING"},
                11: {
                    "state": "open",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "DIRTY",
                },
                12: {
                    "state": "open",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                },
            }
        )
        output = await _run_over_bus(
            bus,
            handler,
            ModelPrInventoryInput(
                repo="OmniNode-ai/omnimarket", pr_numbers=(10, 11, 12)
            ),
        )
        by_number = {s.pr_number: s for s in output.pr_states}
        assert by_number[10].has_conflicts is True
        assert by_number[11].has_conflicts is True
        assert by_number[12].has_conflicts is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_inventory_ci_passing_outcomes_over_bus(
    integration_event_bus: Any,
) -> None:
    """Every ci_passing outcome is reached, including the F3 manual-dispatch filter."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _MockInventoryHandler(
            pr_views={
                n: {"state": "open", "mergeable": "MERGEABLE"} for n in (20, 21, 22, 23)
            },
            check_runs={
                # 20: all PR-associated green -> True
                20: [
                    ModelPrCheckRun(
                        name="build",
                        status="completed",
                        conclusion="success",
                        event="pull_request",
                    ),
                    ModelPrCheckRun(
                        name="lint",
                        status="completed",
                        conclusion="skipped",
                        event="pull_request",
                    ),
                ],
                # 21: one PR-associated failure -> False
                21: [
                    ModelPrCheckRun(
                        name="build",
                        status="completed",
                        conclusion="failure",
                        event="pull_request",
                    ),
                ],
                # 22: only a manually dispatched green -> filtered out -> None
                22: [
                    ModelPrCheckRun(
                        name="CI Summary",
                        status="completed",
                        conclusion="success",
                        event="workflow_dispatch",
                    ),
                ],
                # 23: no checks at all -> None
            },
        )
        output = await _run_over_bus(
            bus,
            handler,
            ModelPrInventoryInput(
                repo="OmniNode-ai/omnimarket", pr_numbers=(20, 21, 22, 23)
            ),
        )
        by_number = {s.pr_number: s for s in output.pr_states}
        assert by_number[20].ci_passing is True
        # Negative control: a real PR-associated failure MUST NOT read as passing.
        assert by_number[21].ci_passing is False
        assert by_number[22].ci_passing is None
        assert by_number[23].ci_passing is None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_inventory_collection_error_is_recorded_not_fatal_over_bus(
    integration_event_bus: Any,
) -> None:
    """A PR whose gh view fails is recorded in collection_errors; the rest survive."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _MockInventoryHandler(
            pr_views={30: {"state": "open", "mergeable": "MERGEABLE"}},
            raise_for=frozenset({31}),
        )
        output = await _run_over_bus(
            bus,
            handler,
            ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(30, 31)),
        )
        assert output.total_collected == 1
        assert len(output.collection_errors) == 1
        assert "#31" in output.collection_errors[0]
    finally:
        await bus.close()


@pytest.mark.integration
async def test_inventory_census_attached_to_terminal_event_over_bus(
    integration_event_bus: Any,
) -> None:
    """The org-wide census rides on the terminal event and gates sweep-done."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = _MockInventoryHandler(
            pr_views={40: {"state": "open", "mergeable": "MERGEABLE"}},
            census=ModelOrgWideOpenPrInventory(open_count=0),
        )
        output = await _run_over_bus(
            bus,
            handler,
            ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(40,)),
        )
        assert output.org_wide_open is not None
        assert output.org_wide_open.sweep_done is True
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Pure census-parse coverage (OMN-13318) — no bus, no subprocess.
# ---------------------------------------------------------------------------


def test_census_parse_single_object() -> None:
    payload = {
        "total_count": 2,
        "items": [
            {
                "number": 101,
                "title": "open a",
                "html_url": "https://github.com/OmniNode-ai/omnibase_infra/pull/101",
                "repository_url": "https://api.github.com/repos/OmniNode-ai/omnibase_infra",
            },
            {
                "number": 102,
                "title": "open b",
                "html_url": "https://github.com/OmniNode-ai/omnimarket/pull/102",
                "repository_url": "https://api.github.com/repos/OmniNode-ai/omnimarket",
            },
        ],
    }
    census = HandlerPrLifecycleInventory._parse_org_wide_open_payload(payload)
    assert census.open_count == 2
    assert census.query_failed is False
    assert census.sweep_done is False
    assert len(census.remainders) == 2
    assert census.remainders[0].repo == "OmniNode-ai/omnibase_infra"


def test_census_parse_paginated_list_merges_items() -> None:
    payload = [
        {"total_count": 3, "items": [{"number": 1, "repository_url": ".../repos/o/r"}]},
        {"items": [{"number": 2, "repository_url": ".../repos/o/r"}]},
    ]
    census = HandlerPrLifecycleInventory._parse_org_wide_open_payload(payload)
    assert census.open_count == 3
    assert len(census.remainders) == 2


def test_census_parse_empty_fails_closed() -> None:
    census = HandlerPrLifecycleInventory._parse_org_wide_open_payload("not-a-dict")
    assert census.query_failed is True
    assert census.sweep_done is False


def test_census_parse_non_int_total_count_defaults_zero() -> None:
    census = HandlerPrLifecycleInventory._parse_org_wide_open_payload(
        {"total_count": "many", "items": []}
    )
    assert census.open_count == 0


def test_repo_from_search_item_without_marker_falls_back_to_org() -> None:
    assert (
        HandlerPrLifecycleInventory._repo_from_search_item({"repository_url": "bogus"})
        == "OmniNode-ai"
    )


# ---------------------------------------------------------------------------
# Pure normalization / extraction coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"state": "completed"}, "completed"),
        ({"state": "queued"}, "queued"),
        ({"state": "in_progress"}, "in_progress"),
        ({"bucket": "fail"}, "completed"),
        ({"bucket": "pending"}, "in_progress"),
        ({"state": "success"}, "completed"),
        ({"state": "weird"}, "weird"),
    ],
)
def test_normalize_check_status(item: dict[str, Any], expected: str) -> None:
    assert HandlerPrLifecycleInventory._normalize_check_status(item) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"conclusion": "SUCCESS"}, "success"),
        ({"bucket": "fail"}, "failure"),
        ({"bucket": "pending"}, None),
        ({"bucket": "skipping"}, "skipped"),
        ({}, None),
    ],
)
def test_normalize_check_conclusion(item: dict[str, Any], expected: str | None) -> None:
    assert HandlerPrLifecycleInventory._normalize_check_conclusion(item) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"event": "pull_request"}, "pull_request"),
        ({"event": None}, None),
        ({"event": "  "}, None),
        ({}, None),
    ],
)
def test_normalize_check_event(item: dict[str, Any], expected: str | None) -> None:
    assert HandlerPrLifecycleInventory._normalize_check_event(item) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (None, True),
        ("pull_request", True),
        ("pull_request_target", True),
        ("workflow_dispatch", False),
        ("merge_group", False),
    ],
)
def test_is_pr_associated(event: str | None, expected: bool) -> None:
    check = ModelPrCheckRun(
        name="c", status="completed", conclusion="success", event=event
    )
    assert HandlerPrLifecycleInventory._is_pr_associated(check) is expected


@pytest.mark.parametrize(
    ("review", "expected"),
    [
        ({"author": "octocat"}, "octocat"),
        ({"author": {"login": "hubber"}}, "hubber"),
        ({}, ""),
    ],
)
def test_extract_review_author(review: dict[str, Any], expected: str) -> None:
    assert HandlerPrLifecycleInventory._extract_review_author(review) == expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"headSha": "abc"}, "abc"),
        ({"headCommitOid": "def"}, "def"),
        ({"headCommit": {"oid": "ghi"}}, "ghi"),
        ({}, None),
    ],
)
def test_extract_merge_queue_head_sha(
    entry: dict[str, Any], expected: str | None
) -> None:
    assert HandlerPrLifecycleInventory._extract_merge_queue_head_sha(entry) == expected


def test_inventory_input_pr_numbers_preserved() -> None:
    """The typed input round-trips its PR numbers so the handler collects exactly
    the requested set."""
    model = ModelPrInventoryInput(repo="OmniNode-ai/omnimarket", pr_numbers=(7, 8, 9))
    assert model.pr_numbers == (7, 8, 9)
