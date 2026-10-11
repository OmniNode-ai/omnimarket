"""The shared cause signature: the annotation read the controller and the bus path both key by."""

from typing import Any

import pytest

from omnimarket.handlers.cause_signature import (
    annotation_query,
    first_failure_annotations,
)


def node(head: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "number": 1,
        "headRefOid": head,
        "statusCheckRollup": {
            "nodes": [
                {"commit": {"statusCheckRollup": {"contexts": {"nodes": contexts}}}}
            ]
        },
    }


def run(name: str, conclusion: str, *annotations: tuple[str, str]) -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": conclusion,
        "annotations": {
            "nodes": [
                {"annotationLevel": lvl, "message": msg} for lvl, msg in annotations
            ]
        },
    }


@pytest.mark.unit
def test_first_failure_annotation_per_failing_check() -> None:
    pr = node(
        "h",
        [
            run("green", "SUCCESS", ("FAILURE", "ignored")),
            run(
                "gate",
                "FAILURE",
                ("WARNING", "a warning"),
                ("FAILURE", "Process completed with exit code 1."),
                ("FAILURE", "  the real failure  "),
            ),
            run("bare", "TIMED_OUT"),
            run("dup", "FAILURE"),
            run("dup", "CANCELLED", ("FAILURE", "second copy")),
            run("dup", "FAILURE", ("FAILURE", "third copy")),
            run("long", "FAILURE", ("FAILURE", "x" * 1200)),
            {"not": "a check run"},
        ],
    )
    assert first_failure_annotations(pr, "h") == {
        "bare": "",
        "dup": "second copy",
        "gate": "the real failure",
        "long": "x" * 1000,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "pr",
    [
        None,
        node("other", []),
        {"headRefOid": "h"},
        {"headRefOid": "h", "statusCheckRollup": {"nodes": []}},
    ],
)
def test_stale_or_malformed_read_is_unread(pr: dict[str, Any] | None) -> None:
    assert first_failure_annotations(pr, "h") is None


@pytest.mark.unit
def test_annotation_query_aliases_each_pr_and_reads_three_annotations() -> None:
    query = annotation_query([("OmniNode-ai/omnimarket", 3662), ("Other-org/lib", 7)])
    assert query.startswith(
        'query { a0: repository(owner: "OmniNode-ai", name: "omnimarket")'
    )
    assert (
        'a1: repository(owner: "Other-org", name: "lib") { pullRequest(number: 7)'
        in query
    )
    assert query.count("annotations(first: 3)") == 2
    assert "contexts(first: 100)" in query
    assert "mutation" not in query
