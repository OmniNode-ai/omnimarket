# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_baseline_cluster_ticket_effect: the signature, the text it admits and the in-memory store."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_inmemory_cluster_ticket_store import (
    InMemoryClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_signature import (
    carries_signature,
    legacy_lines,
    oldest_first,
    search_needles,
    signature_line,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_text import (
    occurrence_comment,
    ticket_description,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_baseline_cluster_ticket import (
    ModelBaselineClusterReportRequest,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketSearchRequest,
)

pytestmark = pytest.mark.unit

REPO = "omnibase_infra"
CLUSTER = "tests.nodes.node_example.test_example"
LANE = "lane-baseline"
GATE = "OMN-19998 AC-1"
OBSERVED_AT = "2026-11-15T00:00:00+00:00"
RUN_URL = "https://ci.example/runs/42"


def report_request(**overrides: Any) -> ModelBaselineClusterReportRequest:
    fields: dict[str, Any] = {
        "repo": REPO,
        "cluster": CLUSTER,
        "failing_count": 2,
        "examples": (
            "tests/node_example/test_example.py::test_a",
            "tests/node_example/test_example.py::test_b",
            "tests/node_example/test_example.py::test_c",
        ),
        "run_url": RUN_URL,
        "observed_at": OBSERVED_AT,
        "requested_by_lane": LANE,
        "parent": "OMN-19998",
        "gate": GATE,
        "diagnosis": "example-org/example-repo#1",
        "timeout_seconds": 0.2,
        "max_duplicate_hops": 3,
        "example_limit": 2,
    }
    fields.update(overrides)
    return ModelBaselineClusterReportRequest(**fields)


def description(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the signature


def test_the_exact_signature_line_is_the_dev_baseline_triple() -> None:
    assert signature_line(REPO, CLUSTER) == (
        f"Cluster signature: dev-baseline:{REPO}:{CLUSTER}"
    )


def test_a_description_with_the_exact_signature_line_matches() -> None:
    text = description("created-by-lane: lane-x", "", signature_line(REPO, CLUSTER))
    assert carries_signature(text, REPO, CLUSTER)


def test_the_legacy_two_line_form_with_trailing_spaces_matches() -> None:
    repo_line, cluster_line = legacy_lines(REPO, CLUSTER)
    assert repo_line.endswith(f"`{REPO}`  ")
    assert cluster_line.endswith(f"`{CLUSTER}`  ")
    assert carries_signature(
        description("title", repo_line, cluster_line), REPO, CLUSTER
    )


def test_a_legacy_form_read_loose_still_matches_lines_are_stripped() -> None:
    repo_line, cluster_line = legacy_lines(REPO, CLUSTER)
    text = description(f"  {repo_line}  ", f"\t{cluster_line}")
    assert carries_signature(text, REPO, CLUSTER)


def test_a_longer_cluster_name_does_not_match() -> None:
    mine = description(signature_line(REPO, CLUSTER), *legacy_lines(REPO, CLUSTER))
    assert not carries_signature(mine, REPO, CLUSTER + "0")
    theirs = description(
        signature_line(REPO, CLUSTER + "0"), *legacy_lines(REPO, CLUSTER + "0")
    )
    assert not carries_signature(theirs, REPO, CLUSTER)


def test_a_legacy_repo_mismatch_does_not_match() -> None:
    text = description(*legacy_lines("omnimarket", CLUSTER))
    assert not carries_signature(text, REPO, CLUSTER)
    assert not carries_signature(text, REPO, CLUSTER + "0")


def test_the_signature_as_a_substring_of_prose_is_not_a_match() -> None:
    prose = description(f"read the {signature_line(REPO, CLUSTER)} line first")
    assert not carries_signature(prose, REPO, CLUSTER)
    prose_legacy = description(f"see {legacy_lines(REPO, CLUSTER)[1]} below")
    assert not carries_signature(prose_legacy, REPO, CLUSTER)


def test_search_needles_are_the_signature_line_and_the_legacy_cluster_line() -> None:
    assert search_needles(REPO, CLUSTER) == (
        f"Cluster signature: dev-baseline:{REPO}:{CLUSTER}",
        f"**Cluster:** `{CLUSTER}`",
    )


def test_oldest_first_orders_by_ticket_number_dedupes_and_puts_unnumbered_last() -> (
    None
):
    assert oldest_first(("OMN-90010", "OMN-9", "OMN-90002", "OMN-90010")) == (
        "OMN-9",
        "OMN-90002",
        "OMN-90010",
    )
    assert oldest_first(("OMN-90010", "TRACKER-XYZ")) == (
        "OMN-90010",
        "TRACKER-XYZ",
    )


# ------------------------------------------------------------------ the text


def test_the_description_opens_with_the_lane_then_the_gate_line() -> None:
    lines = ticket_description(report_request(), None).splitlines()
    assert lines[0] == f"created-by-lane: {LANE}"
    assert lines[1] == ""
    assert f"Gate: {GATE}" in lines


def test_the_description_carries_the_signature_and_the_legacy_lines() -> None:
    text = ticket_description(report_request(), None)
    lines = text.splitlines()
    assert signature_line(REPO, CLUSTER) in lines
    repo_line, cluster_line = legacy_lines(REPO, CLUSTER)
    assert repo_line in lines
    assert cluster_line in lines
    assert carries_signature(text, REPO, CLUSTER)


def test_the_description_lists_the_first_example_limit_examples_then_the_facts() -> (
    None
):
    request = report_request(failing_count=3, example_limit=2)
    lines = ticket_description(request, None).splitlines()
    assert "- `tests/node_example/test_example.py::test_a`" in lines
    assert "- `tests/node_example/test_example.py::test_b`" in lines
    assert not any("::test_c" in line for line in lines)
    assert "Failing: 3" in lines
    assert f"Observed: {OBSERVED_AT}" in lines
    assert f"Run: {RUN_URL}" in lines


def test_the_description_states_the_acceptance_criterion_exactly() -> None:
    lines = ticket_description(report_request(), None).splitlines()
    assert "## Acceptance criteria" in lines
    assert (
        f"- [ ] AC1: the tests of cluster {CLUSTER} in {REPO} pass on dev"
        " -- falsifier: run the cluster's test module on dev and read it fail"
    ) in lines


def test_the_description_names_a_closed_previous_ticket_only_when_given() -> None:
    assert (
        "Previous ticket (closed): OMN-90001"
        in ticket_description(report_request(), "OMN-90001").splitlines()
    )
    assert "Previous ticket (closed)" not in ticket_description(report_request(), None)


def test_the_comment_opens_with_still_failing_and_names_the_occurrence() -> None:
    comment = occurrence_comment(report_request(example_limit=2))
    assert comment == (
        "Still failing: 2 failing test(s)"
        f" at {OBSERVED_AT} (run {RUN_URL}):"
        " `tests/node_example/test_example.py::test_a`,"
        " `tests/node_example/test_example.py::test_b`"
    )


def test_the_comment_names_the_first_example_limit_nodeids_only() -> None:
    comment = occurrence_comment(report_request(example_limit=1))
    assert "::test_a`" in comment
    assert "::test_b" not in comment


# ------------------------------------------------------------------ the in-memory store


def test_the_in_memory_store_satisfies_the_store_protocol() -> None:
    assert isinstance(InMemoryClusterTicketStore(), ProtocolClusterTicketStore)


def test_the_store_creates_tickets_in_order_from_omn_90001() -> None:
    store = InMemoryClusterTicketStore()
    first = store.create(
        ModelClusterTicketCreateRequest(
            title="first", description="first description", parent="OMN-19998"
        )
    )
    second = store.create(
        ModelClusterTicketCreateRequest(
            title="second", description="second description", parent="OMN-19998"
        )
    )
    assert (first.state, first.ticket) == ("created", "OMN-90001")
    assert (second.state, second.ticket) == ("created", "OMN-90002")
    assert store.tickets["OMN-90001"].description == "first description"
    assert store.tickets["OMN-90002"].description == "second description"


def test_the_store_create_never_overwrites_a_seeded_ticket() -> None:
    store = InMemoryClusterTicketStore()
    store.seed("OMN-90001", "already filed")
    created = store.create(
        ModelClusterTicketCreateRequest(
            title="t", description="the new description", parent="OMN-19998"
        )
    )
    assert created.ticket == "OMN-90002"
    assert store.tickets["OMN-90001"].description == "already filed"


def test_the_store_searches_by_needle_substring() -> None:
    store = InMemoryClusterTicketStore()
    store.seed(
        "OMN-90001",
        description=f"Cluster signature: dev-baseline:{REPO}:{CLUSTER}\n",
    )
    store.seed("OMN-90002", description="an unrelated ticket", status="done")
    result = store.search(
        ModelClusterTicketSearchRequest(needles=search_needles(REPO, CLUSTER))
    )
    assert result.state == "answered"
    assert result.tickets == ("OMN-90001",)
    empty = store.search(
        ModelClusterTicketSearchRequest(
            needles=(f"Cluster signature: dev-baseline:{REPO}:{CLUSTER}0",)
        )
    )
    assert empty.tickets == ()


def test_the_store_reads_a_seeded_ticket_with_its_status_and_duplicate_of() -> None:
    store = InMemoryClusterTicketStore()
    store.seed(
        "OMN-90005",
        description="the duplicate",
        status="duplicate",
        duplicate_of="OMN-90001",
    )
    found = store.read(ModelClusterTicketLookupRequest(ticket="OMN-90005"))
    assert (found.state, found.ticket, found.status) == (
        "found",
        "OMN-90005",
        "duplicate",
    )
    assert found.description == "the duplicate"
    assert found.duplicate_of == "OMN-90001"
    missing = store.read(ModelClusterTicketLookupRequest(ticket="OMN-404"))
    assert missing.state == "missing"


def test_the_store_set_status_changes_what_a_read_reports() -> None:
    store = InMemoryClusterTicketStore()

    def status_of(ticket: str) -> str | None:
        return store.read(ModelClusterTicketLookupRequest(ticket=ticket)).status

    store.seed("OMN-90001", "d")
    assert status_of("OMN-90001") == "open"
    store.set_status("OMN-90001", "done")
    assert status_of("OMN-90001") == "done"


def test_the_store_records_comments_per_ticket() -> None:
    store = InMemoryClusterTicketStore()
    store.seed("OMN-90001", "d")
    store.seed("OMN-90002", "other")
    first = store.comment(
        ModelClusterTicketCommentRequest(ticket="OMN-90001", body="Still failing: 2")
    )
    second = store.comment(
        ModelClusterTicketCommentRequest(ticket="OMN-90001", body="Still failing: 3")
    )
    assert (first.state, second.state) == ("commented", "commented")
    assert first.comment_id != second.comment_id
    assert store.comments["OMN-90001"] == ["Still failing: 2", "Still failing: 3"]
    assert store.comments["OMN-90002"] == []


def test_the_store_a_comment_on_a_ticket_it_does_not_hold_fails() -> None:
    store = InMemoryClusterTicketStore()
    result = store.comment(
        ModelClusterTicketCommentRequest(ticket="OMN-99999", body="Still failing: 2")
    )
    assert result.state == "failed"
    assert result.cause is not None
