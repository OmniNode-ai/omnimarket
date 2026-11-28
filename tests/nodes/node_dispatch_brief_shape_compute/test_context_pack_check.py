# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The context pack check of a code brief: block, hash, approval stamp and the kinds of brief that need none."""

from __future__ import annotations

import hashlib

import pytest

from omnimarket.nodes.node_dispatch_brief_shape_compute import (
    NodeDispatchBriefShapeCompute,
)
from omnimarket.nodes.node_dispatch_brief_shape_compute.models.model_dispatch_brief_shape import (
    ModelContextPackCheckRequest,
)

pytestmark = pytest.mark.unit

BODY = "## intent\nbuild the thing\n\n## tests\nrun the focused suite"
STAMP = "2026-11-16T02:30:00Z"


def digest_of(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def pack_block(
    body: str | None = None,
    *,
    digest: str | None = None,
    stamp: str | None = None,
    header: tuple[str, ...] | None = None,
    fence: str = "```",
) -> str:
    body = BODY if body is None else body
    lines = (
        header
        if header is not None
        else (
            f"final_pack_hash={digest or digest_of(body)}",
            f"pack-approved-by=orchestrator {stamp or STAMP}",
        )
    )
    return "CONTEXT PACK\n" + "\n".join(lines) + f"\n\n{fence}pack\n{body}\n{fence}\n"


def approval_row(digest: str, lane: str = "orchestrator-swarm") -> str:
    return (
        f"{STAMP} | STATUS | lane={lane} | "
        f"approved the context pack final_pack_hash={digest}"
    )


def problems(
    task: str,
    rows: tuple[str, ...] | None = (),
    *,
    error: str = "",
    brief: str | None = None,
) -> tuple[str, ...]:
    result = NodeDispatchBriefShapeCompute().check_context_pack(
        ModelContextPackCheckRequest(
            brief=task if brief is None else brief,
            prose=task,
            ledger_rows=rows,
            ledger_error=error,
        )
    )
    return result.problems


def has(found: tuple[str, ...], needle: str) -> bool:
    return any(needle in line for line in found)


def valid_task() -> tuple[str, tuple[str, ...]]:
    return (
        f"Lane kind: ticket\n\n{pack_block()}",
        (approval_row(digest_of(BODY)),),
    )


@pytest.mark.parametrize(
    "task", ["fixture\n", "Lane kind: ticket\n", "Work kind: code\n"]
)
def test_a_brief_with_no_pack_block_is_refused_naming_the_builder(task: str) -> None:
    found = problems(task)
    assert has(found, "a code brief carries no CONTEXT PACK block")
    assert has(found, "node_context_pack_orchestrator")
    assert has(found, "lane_brief_context")


@pytest.mark.parametrize(
    "task",
    [
        "Work kind: read-only\nWork kind: code\n",
        "Work kind: code\nWork kind: document\n",
        "Lane kind: open-ended\nWork kind: code\n",
        "Work kind: document\nLane kind: ticket\nWork kind: code\n",
    ],
)
def test_conflicting_kind_lines_resolve_to_code(task: str) -> None:
    assert has(problems(task), "a code brief carries no CONTEXT PACK block")


@pytest.mark.parametrize("value", ["review", "codee", ""])
def test_an_unknown_work_kind_value_is_refused(value: str) -> None:
    assert has(problems(f"Work kind: {value}\n"), f"unknown Work kind {value!r}")


@pytest.mark.parametrize(
    "task",
    [
        "Lane kind: ticket\nWork kind: read-only\n",
        "Work kind: document\n",
        "Work kind: read-only\nWork kind: document\n",
        "Lane kind: open-ended\n",
    ],
)
def test_a_brief_with_no_code_work_is_asked_for_no_pack(task: str) -> None:
    assert problems(task) == ()


def test_an_open_ended_lane_name_is_asked_for_no_pack() -> None:
    brief = "Lane: carry-diagnose\nfixture\n"
    assert problems(brief) == ()
    assert has(problems("Lane: carry-build\nfixture\n"), "no CONTEXT PACK block")


def test_a_block_without_a_hash_is_refused() -> None:
    task = f"Lane kind: ticket\n\n{pack_block(header=(f'pack-approved-by=orchestrator {STAMP}',))}"
    found = problems(task)
    assert has(found, "the CONTEXT PACK block carries no final_pack_hash")
    assert not has(found, "no pack-approved-by")


def test_a_block_without_an_approval_line_is_refused() -> None:
    task = f"Lane kind: ticket\n\n{pack_block(header=(f'final_pack_hash={digest_of(BODY)}',))}"
    found = problems(task)
    assert has(found, "no pack-approved-by=orchestrator <ledger row stamp> line")
    assert not has(found, "carries no final_pack_hash")


@pytest.mark.parametrize(
    "line",
    [
        "pack-approved-by=orchestrator",
        f"pack-approved-by=operator {STAMP}",
        "pack-approved-by=orchestrator yesterday",
        "pack-approved-by=orchestrator 9999-99-99T99:99:99Z",
    ],
)
def test_an_approval_line_of_the_wrong_shape_is_refused(line: str) -> None:
    digest = digest_of(BODY)
    task = (
        f"Lane kind: ticket\n\n{pack_block(header=(f'final_pack_hash={digest}', line))}"
    )
    found = problems(task, (approval_row(digest),))
    assert has(found, "pack-approved-by")


def test_a_blank_line_after_the_heading_names_the_blank_line_rule() -> None:
    digest = digest_of(BODY)
    task = (
        f"Lane kind: ticket\n\nCONTEXT PACK\n\nfinal_pack_hash={digest}\n"
        f"pack-approved-by=orchestrator {STAMP}\n"
    )
    assert has(problems(task), "a blank line ends the header")


def test_a_header_without_a_body_is_refused() -> None:
    digest = digest_of(BODY)
    task = (
        f"Lane kind: ticket\n\nCONTEXT PACK\nfinal_pack_hash={digest}\n"
        f"pack-approved-by=orchestrator {STAMP}\n"
    )
    assert has(problems(task, (approval_row(digest),)), "carries no pack body")


def test_an_empty_body_is_refused() -> None:
    task = f"Lane kind: ticket\n\n{pack_block(' ')}"
    assert has(
        problems(task, (approval_row(digest_of(" ")),)),
        "the CONTEXT PACK body is empty",
    )


def test_a_body_that_does_not_hash_to_the_declared_hash_is_refused() -> None:
    digest = digest_of(BODY)
    edited = f"Lane kind: ticket\n\n{pack_block(BODY + chr(10) + 'edited after approval', digest=digest)}"
    wrong = f"Lane kind: ticket\n\n{pack_block(digest='a' * 64)}"
    for task in (edited, wrong):
        assert has(
            problems(task, (approval_row(digest),)),
            "the CONTEXT PACK body hashes to sha256",
        )


def test_a_stamp_with_no_ledger_row_is_refused() -> None:
    task, _ = valid_task()
    assert has(
        problems(task, ()),
        f"pack-approved-by names ledger row {STAMP}, but the ledger holds no row",
    )


def test_a_row_naming_another_hash_is_refused() -> None:
    task, _ = valid_task()
    assert has(
        problems(task, (approval_row("b" * 64),)), "does not name final_pack_hash"
    )


def test_a_row_from_another_lane_is_refused() -> None:
    task, _ = valid_task()
    found = problems(task, (approval_row(digest_of(BODY), lane="fix-0700"),))
    assert has(found, "holds no row at that stamp written by an orchestrator lane")


def test_an_unreadable_or_absent_ledger_is_a_refusal_never_a_pass() -> None:
    task, _ = valid_task()
    unreadable = problems(task, None, error="ledger not readable")
    absent = problems(task, None)
    assert len(unreadable) == 1
    assert "pack-approved-by cannot be confirmed" in unreadable[0]
    assert "ledger not readable" in unreadable[0]
    assert len(absent) == 1
    assert "no ledger reader was given" in absent[0]


def test_a_valid_block_with_body_hash_and_ledger_row_passes() -> None:
    task, rows = valid_task()
    assert problems(task, rows) == ()


def test_a_body_with_a_fence_inside_passes_under_a_longer_fence() -> None:
    body = "## intent\n```\ncode\n```\n\n## tests\nrun it"
    task = f"Lane kind: ticket\n\n{pack_block(body, fence='````')}"
    assert "````pack" in task
    assert problems(task, (approval_row(digest_of(body)),)) == ()
