# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of HandlerBaselineClusterTicket with the in-memory store, each state with a control."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_baseline_cluster_ticket_effect.adapters.adapter_inmemory_cluster_ticket_store import (
    InMemoryClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers import (
    HandlerBaselineClusterTicket,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_signature import (
    legacy_lines,
    signature_line,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_baseline_cluster_ticket import (
    ModelBaselineClusterReportRequest,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCommentResult,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketCreateResult,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
    ModelClusterTicketSearchResult,
)

pytestmark = pytest.mark.unit

REPO = "omnimarket"
CLUSTER = "baseline-cluster-a"
PARENT = "OMN-19998"
GATE = "dev-baseline"


def request(**overrides: object) -> ModelBaselineClusterReportRequest:
    base: dict[str, object] = {
        "repo": REPO,
        "cluster": CLUSTER,
        "failing_count": 3,
        "examples": ("test_a.py::test_x", "test_b.py::test_y"),
        "run_url": "https://ci.example.com/run/1",
        "observed_at": "2026-01-01T00:00:00Z",
        "requested_by_lane": "lane-a",
        "parent": PARENT,
        "gate": GATE,
        "diagnosis": "example-org/example-repo#1",
        "timeout_seconds": 5.0,
        "max_duplicate_hops": 3,
        "example_limit": 5,
    }
    base.update(overrides)
    return ModelBaselineClusterReportRequest.model_validate(base)


def handler(store: ProtocolClusterTicketStore) -> HandlerBaselineClusterTicket:
    return HandlerBaselineClusterTicket(store)


def test_two_runs_of_the_same_cluster_make_one_ticket() -> None:
    store = InMemoryClusterTicketStore()
    h = handler(store)
    first = h.handle(request())
    assert first.state == "created"
    assert first.ticket is not None
    assert first.cause is None
    second = h.handle(request())
    assert second.state == "commented"
    assert second.ticket == first.ticket
    assert second.cause is None
    assert len(store.tickets) == 1
    assert len(store.comments[first.ticket]) == 1


def test_open_legacy_format_ticket_is_commented_not_duplicated() -> None:
    store = InMemoryClusterTicketStore()
    legacy_repo, legacy_cluster = legacy_lines(REPO, CLUSTER)
    desc = f"{legacy_repo}\n{legacy_cluster}\n"
    store.seed("OMN-80001", desc, status="open")
    h = handler(store)
    result = h.handle(request())
    assert result.state == "commented"
    assert result.ticket == "OMN-80001"
    assert len(store.tickets) == 1
    assert len(store.comments["OMN-80001"]) == 1


def test_closed_duplicate_linked_to_open_survivor() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80002", sig, status="duplicate", duplicate_of="OMN-80003")
    store.seed("OMN-80003", "some other description", status="open")
    h = handler(store)
    result = h.handle(request())
    assert result.state == "linked"
    assert result.ticket == "OMN-80003"
    assert result.via == "OMN-80002"
    assert len(store.tickets) == 2
    assert len(store.comments["OMN-80003"]) == 1


def test_duplicate_chain_two_hops_ends_on_open_survivor() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80010", sig, status="duplicate", duplicate_of="OMN-80011")
    store.seed("OMN-80011", "x", status="duplicate", duplicate_of="OMN-80012")
    store.seed("OMN-80012", "y", status="open")
    h = handler(store)
    result = h.handle(request())
    assert result.state == "linked"
    assert result.ticket == "OMN-80012"
    assert result.via == "OMN-80010"


def test_chain_longer_than_max_hops_creates_new_ticket() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80020", sig, status="duplicate", duplicate_of="OMN-80021")
    store.seed("OMN-80021", "x", status="duplicate", duplicate_of="OMN-80022")
    store.seed("OMN-80022", "y", status="duplicate", duplicate_of="OMN-80023")
    store.seed("OMN-80023", "z", status="duplicate", duplicate_of="OMN-80024")
    h = handler(store)
    result = h.handle(request(max_duplicate_hops=2))
    assert result.state == "created"
    assert result.ticket is not None
    assert result.ticket not in (
        "OMN-80020",
        "OMN-80021",
        "OMN-80022",
        "OMN-80023",
        "OMN-80024",
    )


def test_cycle_creates_new_ticket() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80030", sig, status="duplicate", duplicate_of="OMN-80031")
    store.seed("OMN-80031", "x", status="duplicate", duplicate_of="OMN-80030")
    h = handler(store)
    result = h.handle(request())
    assert result.state == "created"
    assert result.ticket is not None


def test_closed_done_ticket_created_with_previous_ticket() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80040", sig, status="done")
    h = handler(store)
    result = h.handle(request())
    assert result.state == "created"
    assert result.ticket is not None
    assert result.previous_ticket == "OMN-80040"


def test_different_cluster_name_does_not_match() -> None:
    store = InMemoryClusterTicketStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-80050", sig, status="open")
    h = handler(store)
    result = h.handle(request(cluster=CLUSTER + "-suffix"))
    assert result.state == "created"
    assert result.ticket is not None
    assert result.ticket != "OMN-80050"


class _FailingSearchStore:
    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(state="failed", cause="boom")

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        return ModelClusterTicketLookupResult(state="missing")

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        return ModelClusterTicketCreateResult(state="created", ticket="OMN-99999")

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        return ModelClusterTicketCommentResult(state="commented", comment_id="c1")


def test_search_failure_ends_unavailable() -> None:
    h = handler(_FailingSearchStore())
    result = h.handle(request())
    assert result.state == "unavailable"
    assert result.cause is not None
    assert result.cause.startswith("search-failed:")


class _FailingReadStore:
    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(state="answered", tickets=("OMN-80060",))

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        return ModelClusterTicketLookupResult(state="failed", cause="read-boom")

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        return ModelClusterTicketCreateResult(state="created", ticket="OMN-99999")

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        return ModelClusterTicketCommentResult(state="commented", comment_id="c1")


def test_read_failure_ends_unavailable() -> None:
    h = handler(_FailingReadStore())
    result = h.handle(request())
    assert result.state == "unavailable"
    assert result.cause is not None
    assert result.cause.startswith("read-failed:")


class _RefusingCreateStore:
    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(state="answered", tickets=())

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        return ModelClusterTicketLookupResult(state="missing")

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        return ModelClusterTicketCreateResult(state="refused", cause="gate-refused")

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        return ModelClusterTicketCommentResult(state="commented", comment_id="c1")


def test_create_refused_ends_refused() -> None:
    h = handler(_RefusingCreateStore())
    result = h.handle(request())
    assert result.state == "refused"
    assert result.cause is not None
    assert result.cause.startswith("ticket-refused:")


class _FailingCommentStore:
    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(state="answered", tickets=("OMN-80070",))

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        return ModelClusterTicketLookupResult(
            state="found",
            ticket="OMN-80070",
            status="open",
            description=signature_line(REPO, CLUSTER),
        )

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        return ModelClusterTicketCreateResult(state="created", ticket="OMN-99999")

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        return ModelClusterTicketCommentResult(state="failed", cause="comment-boom")


def test_comment_failure_ends_unavailable() -> None:
    h = handler(_FailingCommentStore())
    result = h.handle(request())
    assert result.state == "unavailable"
    assert result.cause is not None
    assert result.cause.startswith("comment-failed:")


class _SlowStore:
    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        import time

        time.sleep(10)
        return ModelClusterTicketSearchResult(state="answered", tickets=())

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        return ModelClusterTicketLookupResult(state="missing")

    def create(
        self, request: ModelClusterTicketCreateRequest
    ) -> ModelClusterTicketCreateResult:
        return ModelClusterTicketCreateResult(state="created", ticket="OMN-99999")

    def comment(
        self, request: ModelClusterTicketCommentRequest
    ) -> ModelClusterTicketCommentResult:
        return ModelClusterTicketCommentResult(state="commented", comment_id="c1")


def test_search_timeout_ends_unavailable() -> None:
    h = handler(_SlowStore())
    result = h.handle(request(timeout_seconds=0.01))
    assert result.state == "unavailable"
    assert result.cause is not None
    assert result.cause == "search-timeout"


class _BlindSearchStore(InMemoryClusterTicketStore):
    """The old reporter's view: it cannot find the cluster again, so every run reads the cluster as new."""

    def search(
        self, request: ModelClusterTicketSearchRequest
    ) -> ModelClusterTicketSearchResult:
        return ModelClusterTicketSearchResult(state="answered", tickets=())


def test_control_a_reporter_that_cannot_find_the_cluster_again_files_it_every_run() -> (
    None
):
    """The failing control of test_two_runs_of_the_same_cluster_make_one_ticket.

    With the signature search blinded (what the empty baseline file did to the old reporter), two runs of the same
    cluster make two tickets: the one-ticket result above is the search's, not the in-memory store's.
    """
    store = _BlindSearchStore()
    h = handler(store)
    first = h.handle(request())
    second = h.handle(request())
    assert first.state == "created"
    assert second.state == "created"
    assert len(store.tickets) == 2


class _CountingStore(InMemoryClusterTicketStore):
    reads: int = 0

    def read(
        self, request: ModelClusterTicketLookupRequest
    ) -> ModelClusterTicketLookupResult:
        self.reads += 1
        return super().read(request)


def test_an_oldest_open_ticket_ends_the_reads_however_many_closed_copies_follow() -> (
    None
):
    store = _CountingStore()
    sig = signature_line(REPO, CLUSTER)
    store.seed("OMN-70001", sig, status="open")
    for number in range(70002, 70132):
        store.seed(f"OMN-{number}", sig, status="duplicate", duplicate_of="OMN-70001")
    result = handler(store).handle(request())
    assert result.state == "commented"
    assert result.ticket == "OMN-70001"
    assert store.reads == 1
    assert len(store.tickets) == 131
