# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refusals by cause, from records shaped as the hand-off prints them."""

from omnimarket.nodes.node_install_refusal_trace_compute.handlers import (
    HandlerInstallRefusalTraceCompute,
)
from omnimarket.nodes.node_install_refusal_trace_compute.handlers.handler_install_refusal_trace import (
    CAUSE_CLASSES,
)
from omnimarket.nodes.node_install_refusal_trace_compute.models import (
    ModelInstallRefusalTraceRequest,
    ModelRefusalRecord,
)

_HOST = (
    "this PR changes what runs on a host (scripts/ci/x.py) and its proof is not enough:"
)
_TAIL = " Install the pushed branch revision on the lab through the declared installer."


def _rec(source: str, repo_n: str, reason: str) -> ModelRefusalRecord:
    return ModelRefusalRecord(
        source=source, text=f"handoff_row: {repo_n} NOT-READY {reason}"
    )


def _request(*records: ModelRefusalRecord) -> ModelInstallRefusalTraceRequest:
    return ModelInstallRefusalTraceRequest(
        window_start="2026-11-01T00:00:00Z",
        window_end="2026-11-07T00:00:00Z",
        records=records,
    )


def test_classify_counts_each_cause_from_its_record() -> None:
    result = HandlerInstallRefusalTraceCompute().handle(
        _request(
            _rec(
                "a",
                "svc-a#1",
                f"{_HOST} no Lab line, lab comment or STATUS row carries installed=<host>:<revision>.{_TAIL}",
            ),
            _rec(
                "b",
                "svc-a#2",
                f"{_HOST} no Lab line, lab comment or STATUS row carries installed=<host>:<revision>.{_TAIL}",
            ),
            _rec(
                "c",
                "svc-a#3",
                f"{_HOST} installed=h053:abc1234 names no run: the observation does not say the installed change ran.{_TAIL}",
            ),
            _rec(
                "d",
                "omnibase_infra#4",
                f"{_HOST} installed=h051:abc1234 is not the live head def5678 and does not contain it: install the pushed head.{_TAIL}",
            ),
            _rec(
                "e",
                "omnibase_infra#5",
                "the body has no lab line (host, lane, command, observation) and no lab comment names head 7806d3b: post one as a PR comment",
            ),
            _rec("f", "omnimarket#6", "isDraft=true: flip it ready first"),
        )
    )
    counts = {c.cause: c.count for c in result.classes}
    assert counts == {
        "no-installed-claim": 2,
        "observation-names-no-run": 1,
        "installed-not-live-head": 1,
        "no-lab-line": 1,
        "draft-not-ready": 1,
    }
    assert result.total == 6
    assert result.install_proof_total == 5
    assert result.classes[0].cause == "no-installed-claim"
    assert result.classes[0].sources == ("a", "b")
    assert result.unclassified == ()


def test_classify_lists_a_class_no_record_reached_as_unread_not_zero() -> None:
    result = HandlerInstallRefusalTraceCompute().handle(
        _request(
            _rec(
                "a",
                "svc-b#1",
                f"{_HOST} no Lab line, lab comment or STATUS row carries installed=<host>:<revision>.",
            )
        )
    )
    assert "installed-revision-unread" in result.unread_classes
    assert "no-installed-claim" not in result.unread_classes
    assert all(c.cause != "installed-revision-unread" for c in result.classes)
    assert set(result.unread_classes) | {c.cause for c in result.classes} == {
        c.cause for c in CAUSE_CLASSES
    }


def test_classify_names_a_record_no_class_matches_instead_of_guessing() -> None:
    result = HandlerInstallRefusalTraceCompute().handle(
        _request(
            _rec("odd", "svc-b#9", "a reason no class declares"),
            ModelRefusalRecord(source="noise", text="not a refusal line"),
        )
    )
    assert result.classes == ()
    assert result.unclassified == ("odd", "noise")
    assert result.total == 2


def test_classify_reads_an_unreadable_proof_as_its_own_class() -> None:
    result = HandlerInstallRefusalTraceCompute().handle(
        _request(
            _rec(
                "u",
                "omnimarket#7",
                f"{_HOST} installed=h053:abc1234 is UNREAD: the clone cannot read the head.",
            )
        )
    )
    assert [c.cause for c in result.classes] == ["installed-revision-unread"]
