# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append through the canonical ledger handler, replay, and race real processes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.model_ask_state_transition import (
    ModelAskStateTransition,
    ModelAskStateTransitionRequest,
)
from omnimarket.nodes.node_ask_state_transition_effect.handlers.handler_ask_state_transition import (
    HandlerAskStateTransition,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    local_serve_handler,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from tests.unit.nodes.node_ask_state_compute.test_item_transition import (
    REVISION,
    check,
    claim,
    snapshot,
    terminal,
)

pytestmark = pytest.mark.unit


class FileRunner:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, rows: str) -> ModelAppendCommandResult:
        with self.path.open("a") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


class LedgerPort:
    def __init__(self, path: Path) -> None:
        self.handler = local_serve_handler(
            FileRunner(path), path, "lab-ledger", ("developer",), None
        )

    async def append(self, request, *, timeout_s):
        return self.handler.handle(request, writer_principal="developer")


def command(*rows: str, transitions=(), checks=()) -> ModelAskStateTransitionRequest:
    return ModelAskStateTransitionRequest(
        snapshot=snapshot(*rows, checks=checks),
        transitions=transitions,
        requested_by_lane="fixture",
        requesting_host="lab",
    )


def parked(
    *, run="run-a", revision=REVISION, stamp="2026-11-04T12:04:00Z"
) -> ModelAskStateTransition:
    return ModelAskStateTransition(
        item_id="cap-item",
        state="parked",
        evidence="",
        revision=revision,
        run_id=run,
        lane="builder",
        owner="accountable",
        at=datetime.fromisoformat(stamp.replace("Z", "+00:00")),
        reason="waiting for review",
        review_by=datetime(2026, 11, 5, tzinfo=UTC),
        seq=2,
    )


async def test_item_transition_terminal_consumption_and_replay(tmp_path: Path) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal() + "\n")
    handler = HandlerAskStateTransition(appender=LedgerPort(ledger))
    first = await handler.handle(command(claim(), terminal(), checks=(check(),)))
    first_copy = ledger.read_bytes()
    assert len(first.rows) == 2
    assert first.state.asks[0].state.value == "done"
    assert all(
        "revision=" + REVISION in row and "run=run-a" in row for row in first.rows
    )
    replay = await handler.handle(
        command(*ledger.read_text().splitlines(), checks=(check(),))
    )
    assert ledger.read_bytes() == first_copy
    assert replay.state == first.state
    assert {r.status.value for r in replay.receipts} <= {"already-present", "duplicate"}


async def test_item_transition_parked_row_has_review_and_sequence(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n")
    handler = HandlerAskStateTransition(appender=LedgerPort(ledger))
    first = await handler.handle(command(claim(), transitions=(parked(),)))
    assert len(first.rows) == 1
    assert "state=parked" in first.rows[0]
    assert "reason=waiting for review" in first.rows[0]
    assert "review_by=2026-11-05T00:00:00Z" in first.rows[0]
    assert "seq=2" in first.rows[0]
    got = first.state.asks[0]
    assert got.state.value == "parked"
    assert got.owner_lane == "accountable"
    assert got.running_lane is None
    assert got.review_by == datetime(2026, 11, 5, tzinfo=UTC)
    before = ledger.read_bytes()
    replay = await handler.handle(
        command(*ledger.read_text().splitlines(), transitions=(parked(),))
    )
    assert ledger.read_bytes() == before
    assert replay.state == first.state


@pytest.mark.parametrize("outcome", ["failed", "lane-error", "blocked"])
async def test_item_transition_failed_terminal_consumed_in_same_fold(
    tmp_path: Path, outcome: str
) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal(outcome) + "\n")
    result = await HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(
        command(claim(), terminal(outcome))
    )
    got = result.state.asks[0]
    assert got.state.value == "owned"
    assert got.owner_lane == "accountable"
    assert got.running_lane is None
    assert got.orphaned
    assert len(result.rows) == 2


async def test_item_transition_rejects_open_pr_completion(tmp_path: Path) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal() + "\n")
    result = await HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(
        command(claim(), terminal(), checks=(check(False),))
    )
    assert all("state=done" not in row for row in result.rows)
    assert result.state.asks[0].state.value == "owned"


async def test_item_transition_stale_intent_is_not_appended(tmp_path: Path) -> None:
    replacement = claim("run-b", "replacement", "2026-11-04T12:02:00Z")
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + replacement + "\n")
    before = ledger.read_bytes()
    result = await HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(
        command(claim(), replacement, transitions=(parked(),))
    )
    assert result.skipped
    assert result.rows == ()
    assert ledger.read_bytes() == before
    assert result.state.asks[0].running_lane == "replacement"


async def test_item_transition_missing_park_fields_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n")
    incomplete = parked().model_copy(update={"review_by": None})
    result = await HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(
        command(claim(), transitions=(incomplete,))
    )
    assert not result.rows
    assert result.skipped


def _race_worker(path: str, payload: str, barrier, results) -> None:
    request = ModelAskStateTransitionRequest.model_validate_json(payload)
    barrier.wait(timeout=30)
    result = asyncio.run(
        HandlerAskStateTransition(appender=LedgerPort(Path(path))).handle(request)
    )
    results.put((tuple(r.status.value for r in result.receipts), result.errors))


@pytest.mark.parametrize("same_transition", [True, False])
def test_item_transition_two_process_race_on_scratch_ledger(
    tmp_path: Path, same_transition: bool
) -> None:
    import multiprocessing

    from omnimarket.nodes.node_ask_state_compute.node import NodeAskStateCompute

    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n")
    first = parked()
    second = (
        first
        if same_transition
        else first.model_copy(
            update={
                "state": "done",
                "evidence": "sample#42",
                "at": datetime(2026, 11, 4, 12, 5, tzinfo=UTC),
            }
        )
    )
    requests = [
        command(claim(), transitions=(change,), checks=(check(),))
        for change in (first, second)
    ]
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_worker,
            args=(str(ledger), request.model_dump_json(), barrier, results),
        )
        for request in requests
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=45)
            assert process.exitcode == 0
        observations = [results.get(timeout=5) for _ in processes]
        assert all(not errors for _, errors in observations)
        rows = ledger.read_text().splitlines()
        statuses = [row for row in rows if " | STATUS | " in row]
        assert len(statuses) == (1 if same_transition else 2)
        if same_transition:
            assert sorted(
                status for receipts, _ in observations for status in receipts
            ) == ["accepted", "duplicate"]
        got = NodeAskStateCompute().handle(snapshot(*rows, checks=(check(),))).asks[0]
        assert got.state.value == ("parked" if same_transition else "done")
        assert got.owner_lane == "accountable"
        # ClosedHasEvidence: every closed projection has the checked evidence of its run/revision.
        if got.state.value == "done":
            assert got.evidence
            assert got.revision == REVISION
            assert got.claim_run_id == "run-a"
        before = ledger.read_bytes()
        for request in requests:
            asyncio.run(
                HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(request)
            )
        assert ledger.read_bytes() == before
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_id", "another-item"),
        ("state", "done"),
        ("evidence", "receipt:changed"),
        ("revision", "changed-revision"),
        ("run_id", "replacement-run"),
    ],
)
def test_item_transition_id_contains_all_five_fields(field: str, value: str) -> None:
    from omnimarket.nodes.node_ask_state_transition_effect.handlers.handler_ask_state_transition import (
        _row_id,
    )

    intent = parked()
    request = command(claim())
    assert _row_id(intent, request) != _row_id(
        intent.model_copy(update={field: value}), request
    )


@pytest.mark.parametrize("pr_state", ["OPEN", "CLOSED", "MERGED"])
async def test_item_transition_checks_watcher_pr_before_closure(
    tmp_path: Path, pr_state: str
) -> None:
    import json

    from omnimarket.handlers.handler_ask_completion_evidence import (
        FileCompletionChecker,
    )
    from omnimarket.models.model_ask_state_transition import ModelAskCompletionSources

    watcher = tmp_path / "watcher.json"
    watcher.write_text(
        json.dumps(
            {
                "last_tick": "2026-11-04T13:00:00Z",
                "prs": {
                    "sample#42": {
                        "facts": {
                            "state": pr_state,
                            "merged_at": "2026-11-04T12:02:00Z"
                            if pr_state == "MERGED"
                            else "",
                        }
                    }
                },
            }
        )
    )
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal() + "\n")
    checker = FileCompletionChecker(
        ModelAskCompletionSources(watcher_state_path=watcher, max_age_seconds=480)
    )
    handler = HandlerAskStateTransition(
        appender=LedgerPort(ledger), completion_checker=checker
    )
    # Even a caller claiming verified=True must not override the actual source.
    result = await handler.handle(command(claim(), terminal(), checks=(check(),)))
    assert result.state.asks[0].state.value == (
        "done" if pr_state == "MERGED" else "owned"
    )
    assert any("state=done" in row for row in result.rows) == (pr_state == "MERGED")


async def test_item_transition_expired_watcher_cannot_complete(tmp_path: Path) -> None:
    import json

    from omnimarket.handlers.handler_ask_completion_evidence import (
        FileCompletionChecker,
    )
    from omnimarket.models.model_ask_state_transition import ModelAskCompletionSources

    watcher = tmp_path / "watcher.json"
    watcher.write_text(
        json.dumps(
            {
                "last_tick": "2026-11-04T12:00:00Z",
                "prs": {
                    "sample#42": {
                        "facts": {
                            "state": "MERGED",
                            "merged_at": "2026-11-04T11:59:00Z",
                        }
                    }
                },
            }
        )
    )
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + terminal() + "\n")
    checker = FileCompletionChecker(
        ModelAskCompletionSources(watcher_state_path=watcher, max_age_seconds=480)
    )
    result = await HandlerAskStateTransition(
        appender=LedgerPort(ledger), completion_checker=checker
    ).handle(command(claim(), terminal()))
    assert result.state.asks[0].state.value == "owned"


@pytest.mark.parametrize(
    "bad_terminal",
    [
        terminal(lane="foreign"),
        terminal().replace(
            "closes-CLAIM=2026-11-04T12:01:00Z", "closes-CLAIM=2026-11-04T11:59:00Z"
        ),
    ],
)
async def test_item_transition_consumer_refuses_terminal_of_another_claim(
    tmp_path: Path, bad_terminal: str
) -> None:
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + bad_terminal + "\n")
    result = await HandlerAskStateTransition(appender=LedgerPort(ledger)).handle(
        command(claim(), bad_terminal, checks=(check(),))
    )
    assert all("state=done" not in row for row in result.rows)
    assert result.state.asks[0].state.value == "owned"
    assert result.state.asks[0].running_lane == "builder"


@pytest.mark.parametrize(
    ("receipt_run", "receipt_revision", "outcome", "expected"),
    [
        ("run-a", REVISION, "done", "done"),
        ("run-b", REVISION, "done", "owned"),
        ("run-a", "old-revision", "done", "owned"),
        ("run-a", REVISION, "failed", "owned"),
    ],
)
async def test_item_transition_checks_receipt_content_and_run(
    tmp_path: Path, receipt_run: str, receipt_revision: str, outcome: str, expected: str
) -> None:
    import json

    from omnimarket.handlers.handler_ask_completion_evidence import (
        FileCompletionChecker,
    )
    from omnimarket.models.model_ask_state_transition import ModelAskCompletionSources

    (tmp_path / "completion.json").write_text(
        json.dumps(
            {"revision": receipt_revision, "run_id": receipt_run, "outcome": outcome}
        )
    )
    source = terminal(evidence="receipt:completion.json")
    ledger = tmp_path / "scratch-ledger.md"
    ledger.write_text(claim() + "\n" + source + "\n")
    checker = FileCompletionChecker(
        ModelAskCompletionSources(receipt_root=tmp_path, max_age_seconds=480)
    )
    result = await HandlerAskStateTransition(
        appender=LedgerPort(ledger), completion_checker=checker
    ).handle(command(claim(), source))
    assert result.state.asks[0].state.value == expected
