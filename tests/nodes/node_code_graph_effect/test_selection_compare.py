# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""compare_selection: the graph's reach beside the parse selection, with a hard deadline (OMN-26273)."""

from __future__ import annotations

import multiprocessing
import os
import signal
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import neo4j  # noqa: F401 - imported here so the forked worker does not pay for it inside the deadline
import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFallbackReason,
    EnumLookupServedFrom,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
    ModelLookupMatch,
)
from omnimarket.models.model_code_graph_selection_compare import (
    EnumCompareOutcome,
    EnumGraphAnswerSource,
    ModelParseSelection,
    ModelSelectionCompareCommand,
    ModelSelectionCompareRow,
)
from omnimarket.models.model_code_graph_selection_verdict import (
    EnumPartOneVerdict,
    EnumPartTwoVerdict,
    ModelLaterNightlyFailure,
    ModelSelectionVerdict,
    ModelSelectionVerdictRequest,
)
from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphUnreachableError,
    open_memgraph_store,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_deadline_worker import (
    run_with_deadline,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_selection_compare import (
    SelectionCompare,
    memgraph_reach,
    render_row,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_selection_verdict import (
    SelectionVerdict,
)
from omnimarket.nodes.node_code_graph_effect.handlers.handler_code_graph_requested import (
    HandlerCodeGraphCompareSelectionRequested,
    HandlerCodeGraphSelectionVerdictRequested,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from tests.nodes.node_code_graph_effect.code_graph_support import (
    CORE,
    EXAMPLE,
    make_clone,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_code_graph_effect"
)
POLICY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_test_selection_selector_compute/policy.yaml"
)
BASE = "b" * 40
HEAD = "h" * 40
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _is_test(path: str) -> bool:
    return path.startswith("tests/")


def _answer(
    commit: str | None = BASE,
    paths: tuple[str, ...] = (),
    *,
    served_from: EnumLookupServedFrom = EnumLookupServedFrom.MEMGRAPH,
    status: str = "found",
) -> ModelCodeGraphLookupResult:
    return ModelCodeGraphLookupResult(
        correlation_id="c",
        question=EnumCodeGraphQuestion.REACH,
        repo="omnimarket",
        status=status,  # type: ignore[arg-type]
        answered_from=commit,
        matches=tuple(ModelLookupMatch(path=p) for p in paths),
        served_from=served_from,
    )


def _command(**changes: object) -> ModelSelectionCompareCommand:
    fields: dict[str, object] = {
        "correlation_id": "pr-1",
        "repo": "omnimarket",
        "pr_number": 1,
        "head_sha": HEAD,
        "base_sha": BASE,
        "changed_files": ("src/omnimarket/a.py",),
        "parse": ModelParseSelection(
            stage=0,
            selected_paths=("tests/a/",),
            selector_version="1.0.0",
            policy_version="p1",
        ),
        "test_files": (
            "tests/a/test_one.py",
            "tests/a/test_two.py",
            "tests/b/test_three.py",
        ),
    }
    fields.update(changes)
    return ModelSelectionCompareCommand.model_validate(fields)


def _compare(
    *,
    memgraph: Callable[[ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult],
    files: Callable[[ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult]
    | None = None,
    lookup_deadline: float = 2.0,
    fallback_deadline: float = 2.0,
    appender: object | None = None,
) -> SelectionCompare:
    return SelectionCompare(
        memgraph_lookup=memgraph,
        file_lookup=files
        or (lambda _command: _answer(served_from=EnumLookupServedFrom.FILE_STORE)),
        is_test=_is_test,
        lookup_deadline_seconds=lookup_deadline,
        fallback_deadline_seconds=fallback_deadline,
        appender=appender,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


# -- the hung Bolt peer ------------------------------------------------------------------


class _Silent(socketserver.BaseRequestHandler):
    """Accept the connection and never answer; return (closing the socket) when the client goes."""

    def handle(self) -> None:
        while self.request.recv(4096):
            pass


@contextmanager
def _hung_bolt_peer() -> Iterator[str]:
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", 0), _Silent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"bolt://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _bolt_lookup(
    uri: str,
) -> Callable[[ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult]:
    return lambda command: memgraph_reach(
        lambda: open_memgraph_store(resolve_uri=lambda: uri), command
    )


def _sockets() -> int:
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{name}").startswith("socket:"):
                count += 1
        except OSError:
            continue
    return count


def test_hung_bolt_deadline_returns_the_file_store_answer_with_a_typed_reason() -> None:
    with _hung_bolt_peer() as uri:
        compare = _compare(
            memgraph=_bolt_lookup(uri), lookup_deadline=1.0, fallback_deadline=1.0
        )
        started = time.monotonic()
        row = compare.compare(_command())
        elapsed = time.monotonic() - started
    assert elapsed < 1.0 + 1.0 + 1.0
    assert row.graph_source is EnumGraphAnswerSource.FILE_STORE
    assert row.fallback_reason is EnumLookupFallbackReason.READ_TIMED_OUT
    assert [d.stage for d in row.deadlines] == ["memgraph"]
    assert not row.valid_for_verdict


def test_hung_bolt_deadline_with_a_hung_file_store_is_a_typed_error_inside_the_bound() -> (
    None
):
    def hung_files(_command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
        time.sleep(600)
        raise AssertionError("the worker was not terminated")

    with _hung_bolt_peer() as uri:
        compare = _compare(
            memgraph=_bolt_lookup(uri),
            files=hung_files,
            lookup_deadline=1.0,
            fallback_deadline=1.0,
        )
        started = time.monotonic()
        row = compare.compare(_command())
        elapsed = time.monotonic() - started
    assert elapsed < 1.0 + 1.0 + 1.0
    assert row.outcome is EnumCompareOutcome.LOOKUP_FAILED
    assert [d.stage for d in row.deadlines] == ["memgraph", "file-store"]
    assert row.reason is not None
    assert row.graph_only == row.parse_only == row.both == 0


def test_hung_bolt_deadline_twenty_hangs_leave_no_worker_and_no_socket() -> None:
    with _hung_bolt_peer() as uri:
        compare = _compare(
            memgraph=_bolt_lookup(uri), lookup_deadline=0.3, fallback_deadline=0.3
        )
        compare.compare(_command())  # warm up, so the baseline holds the lazy handles
        sockets_before = _sockets()
        for _ in range(20):
            row = compare.compare(_command())
            assert row.fallback_reason is EnumLookupFallbackReason.READ_TIMED_OUT
        assert multiprocessing.active_children() == []
        settle = time.monotonic() + 3.0
        while _sockets() > sockets_before and time.monotonic() < settle:
            time.sleep(0.05)
        assert _sockets() <= sockets_before


def test_hung_bolt_deadline_kills_a_worker_that_ignores_termination(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"

    def stubborn() -> str:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pid_file.write_text(str(os.getpid()))
        time.sleep(600)
        return "never"

    outcome = run_with_deadline(stubborn, 0.5)
    assert outcome.status == "deadline"
    assert outcome.elapsed_seconds < 0.5 + 1.0 + 1.0
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_hung_bolt_deadline_a_refused_connection_falls_back_with_its_own_reason() -> (
    None
):
    def refused(_command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
        raise MemgraphUnreachableError(
            EnumLookupFallbackReason.CONNECTION_REFUSED, "refused"
        )

    row = _compare(memgraph=refused).compare(_command())
    assert row.fallback_reason is EnumLookupFallbackReason.CONNECTION_REFUSED
    assert row.deadlines == ()
    assert row.graph_source is EnumGraphAnswerSource.FILE_STORE


# -- the counts --------------------------------------------------------------------------


def test_compare_selection_counts_graph_only_parse_only_and_both() -> None:
    reach = _answer(paths=("tests/a/test_one.py", "tests/b/test_three.py", "src/x.py"))
    row = _compare(memgraph=lambda _c: reach).compare(_command())
    assert row.outcome is EnumCompareOutcome.COMPARED
    assert (row.graph_only, row.parse_only, row.both) == (1, 1, 1)
    assert row.graph_only_paths == ("tests/b/test_three.py",)
    assert row.valid_for_verdict
    assert row.indexed_commit == BASE


def test_compare_selection_a_changed_test_file_counts_as_reached() -> None:
    row = _compare(memgraph=lambda _c: _answer()).compare(
        _command(changed_files=("tests/a/test_one.py",))
    )
    assert (row.graph_only, row.parse_only, row.both) == (0, 1, 1)


def test_compare_selection_the_whole_suite_leaves_no_graph_only_test() -> None:
    parse = ModelParseSelection(
        stage=0,
        selected_paths=("tests/",),
        selector_version="1.0.0",
        policy_version="p1",
    )
    reach = _answer(paths=("tests/b/test_three.py",))
    row = _compare(memgraph=lambda _c: reach).compare(_command(parse=parse))
    assert (row.graph_only, row.parse_only, row.both) == (0, 2, 1)


def test_compare_selection_snapshot_mismatch_is_in_no_count() -> None:
    reach = _answer(commit="c" * 40, paths=("tests/b/test_three.py",))
    row = _compare(memgraph=lambda _c: reach).compare(_command())
    assert row.outcome is EnumCompareOutcome.SNAPSHOT_MISMATCH
    assert (row.graph_only, row.parse_only, row.both) == (0, 0, 0)
    assert row.graph_only_paths == ()
    assert not row.valid_for_verdict


def test_compare_selection_a_row_missing_a_count_fails_the_model() -> None:
    row = _compare(memgraph=lambda _c: _answer()).compare(_command())
    fields = row.model_dump(exclude={"valid_for_verdict"})
    for count in ("graph_only", "parse_only", "both"):
        broken = {k: v for k, v in fields.items() if k != count}
        with pytest.raises(ValidationError):
            ModelSelectionCompareRow.model_validate(broken)


def test_compare_selection_a_mismatch_row_with_a_count_fails_the_model() -> None:
    row = _compare(memgraph=lambda _c: _answer(commit="c" * 40)).compare(_command())
    fields = row.model_dump(exclude={"valid_for_verdict"})
    with pytest.raises(ValidationError):
        ModelSelectionCompareRow.model_validate({**fields, "graph_only": 1})


def test_compare_selection_a_file_store_answer_is_excluded_from_the_verdict() -> None:
    def refused(_command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
        raise MemgraphUnreachableError(EnumLookupFallbackReason.UNREACHABLE, "down")

    files = lambda _c: _answer(  # noqa: E731
        paths=("tests/b/test_three.py",), served_from=EnumLookupServedFrom.FILE_STORE
    )
    row = _compare(memgraph=refused, files=files).compare(_command())
    assert row.outcome is EnumCompareOutcome.COMPARED
    assert row.graph_source is EnumGraphAnswerSource.FILE_STORE
    assert row.graph_only == 1
    assert not row.valid_for_verdict


def test_compare_selection_a_historical_merge_is_labelled_and_counts_none() -> None:
    reach = _answer(paths=("tests/b/test_three.py",))
    row = _compare(memgraph=lambda _c: reach).compare(_command(historical=True))
    assert row.outcome is EnumCompareOutcome.HISTORICAL_UNVERSIONED
    assert (row.graph_only, row.parse_only, row.both) == (0, 0, 0)


def test_compare_selection_a_refused_statement_is_a_typed_failure_not_a_fallback() -> (
    None
):
    def refused(_command: ModelCodeGraphLookupCommand) -> ModelCodeGraphLookupResult:
        raise RuntimeError("Unknown function")

    row = _compare(memgraph=refused).compare(_command())
    assert row.outcome is EnumCompareOutcome.LOOKUP_FAILED
    assert row.graph_source is EnumGraphAnswerSource.NONE
    assert "Unknown function" in (row.reason or "")


class _Appender:
    def __init__(self, status: str = "accepted") -> None:
        self.rows: list[str] = []
        self._status = status

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.rows.append(rows)
        return ModelAppendOutcome(status=self._status)  # type: ignore[arg-type]


def test_compare_selection_appends_one_row_through_the_appender() -> None:
    appender = _Appender()
    result = _compare(
        memgraph=lambda _c: _answer(paths=("tests/b/test_three.py",)),
        appender=appender,
    ).handle(_command())
    assert result.ledger == "written"
    (text,) = appender.rows
    assert text.count("\n") == 1
    assert " | STATUS | " in text
    for cell in ("graph_only=1", "parse_only=2", "both=0", "valid=yes", "gate=none"):
        assert f" | {cell} | " in text
    assert text.rstrip("\n") == render_row(result.row)


def test_compare_selection_an_append_that_does_not_land_is_a_typed_failure() -> None:
    result = _compare(
        memgraph=lambda _c: _answer(), appender=_Appender("refused")
    ).handle(_command())
    assert result.ledger == "append-failed"
    assert result.failure is not None


def test_compare_selection_writes_nothing_unless_asked() -> None:
    result = _compare(memgraph=lambda _c: _answer()).handle(_command())
    assert result.ledger == "not-written"


# -- the verdict -------------------------------------------------------------------------


def _row(
    n: int,
    *,
    graph_only: tuple[str, ...] = (),
    source: EnumGraphAnswerSource = EnumGraphAnswerSource.MEMGRAPH,
    outcome: EnumCompareOutcome = EnumCompareOutcome.COMPARED,
) -> ModelSelectionCompareRow:
    return ModelSelectionCompareRow(
        correlation_id=f"pr-{n}",
        repo="omnimarket",
        pr_number=n,
        base_sha=BASE,
        head_sha=HEAD,
        compared_at=NOW,
        outcome=outcome,
        compared_stage=0,
        selector_version="1.0.0",
        policy_version="p1",
        graph_source=source,
        indexed_commit=BASE,
        graph_only=len(graph_only) if outcome is EnumCompareOutcome.COMPARED else 0,
        parse_only=0,
        both=0,
        graph_only_paths=graph_only if outcome is EnumCompareOutcome.COMPARED else (),
        elapsed_seconds=0.1,
    )


def _verdict(
    rows: tuple[ModelSelectionCompareRow, ...],
    *,
    days: int = 7,
    minimum: int = 3,
    nights: int | None = None,
    failures: tuple[ModelLaterNightlyFailure, ...] = (),
) -> ModelSelectionVerdict:
    return SelectionVerdict().handle(
        ModelSelectionVerdictRequest(
            rows=rows,
            window_start=NOW - timedelta(days=days),
            now=NOW,
            graph_min_valid_comparisons=minimum,
            nightly_nights_observed=nights,
            nightly_failures=failures,
        )
    )


def test_verdict_part_one_is_pending_inside_the_window() -> None:
    verdict = _verdict((_row(1),), days=6)
    assert verdict.part_one is EnumPartOneVerdict.PENDING


def test_verdict_part_one_is_insufficient_under_the_minimum_never_a_pass() -> None:
    verdict = _verdict((_row(1, graph_only=("tests/x/test_a.py",)), _row(2)))
    assert verdict.part_one is EnumPartOneVerdict.INSUFFICIENT
    assert verdict.valid_comparisons == 2


def test_verdict_part_one_names_a_graph_found_test() -> None:
    rows = (_row(1, graph_only=("tests/x/test_a.py",)), _row(2), _row(3))
    assert _verdict(rows).part_one is EnumPartOneVerdict.GRAPH_FOUND_TESTS_PARSE_MISSED


def test_verdict_part_one_finds_nothing_extra() -> None:
    rows = (_row(1), _row(2), _row(3))
    assert _verdict(rows).part_one is EnumPartOneVerdict.GRAPH_FOUND_NOTHING_EXTRA


def test_verdict_excludes_mismatch_file_store_historical_and_failed_rows() -> None:
    rows = (
        _row(1),
        _row(2, outcome=EnumCompareOutcome.SNAPSHOT_MISMATCH),
        _row(3, source=EnumGraphAnswerSource.FILE_STORE, graph_only=("tests/x/t.py",)),
        _row(4, outcome=EnumCompareOutcome.HISTORICAL_UNVERSIONED),
        _row(5, outcome=EnumCompareOutcome.LOOKUP_FAILED),
    )
    verdict = _verdict(rows, minimum=1)
    assert verdict.valid_comparisons == 1
    assert verdict.graph_only_total == 0
    assert (
        verdict.excluded_snapshot_mismatch,
        verdict.excluded_file_store,
        verdict.excluded_historical,
        verdict.excluded_lookup_failed,
    ) == (1, 1, 1, 1)


def test_verdict_refuses_unknown_for_part_one_after_seven_days() -> None:
    fields = _verdict((_row(1),)).model_dump()
    with pytest.raises(ValidationError, match="part one is still pending"):
        ModelSelectionVerdict.model_validate(
            {**fields, "part_one": "pending", "days_elapsed": 7}
        )
    with pytest.raises(ValidationError):
        ModelSelectionVerdict.model_validate({**fields, "part_one": "unknown"})


def test_verdict_refuses_unknown_for_part_two_after_the_seventh_night() -> None:
    fields = _verdict((_row(1),), nights=7).model_dump()
    with pytest.raises(ValidationError, match="part two is still pending"):
        ModelSelectionVerdict.model_validate(
            {**fields, "part_two": "pending", "nights_observed": 7}
        )
    with pytest.raises(ValidationError):
        ModelSelectionVerdict.model_validate({**fields, "part_two": "unknown"})


def test_verdict_part_two_is_pending_until_the_seventh_night() -> None:
    rows = (_row(1, graph_only=("tests/x/test_a.py",)),)
    assert _verdict(rows, nights=6).part_two is EnumPartTwoVerdict.PENDING
    assert _verdict(rows, nights=None).part_two is EnumPartTwoVerdict.PENDING
    assert (
        _verdict(rows, nights=7).part_two
        is EnumPartTwoVerdict.NO_GRAPH_ONLY_TEST_FAILED
    )


def test_verdict_part_two_names_a_graph_only_test_a_nightly_failed() -> None:
    rows = (_row(1, graph_only=("tests/x/test_a.py",)), _row(2))
    failures = (
        ModelLaterNightlyFailure(
            repo="omnimarket", pr_number=1, test_path="tests/x/test_a.py"
        ),
        ModelLaterNightlyFailure(
            repo="omnimarket", pr_number=2, test_path="tests/x/test_other.py"
        ),
    )
    verdict = _verdict(rows, nights=2, failures=failures)
    assert verdict.part_two is EnumPartTwoVerdict.GRAPH_ONLY_TEST_FAILED
    assert verdict.graph_only_failures == ("omnimarket#1:tests/x/test_a.py",)


def test_verdict_part_two_is_not_applicable_without_a_graph_only_test() -> None:
    assert _verdict((_row(1),), nights=7).part_two is EnumPartTwoVerdict.NOT_APPLICABLE


# -- the contract and the settings -------------------------------------------------------


def test_compare_selection_contract_routes_both_operations() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    routed = {h["operation"]: h for h in contract["handler_routing"]["handlers"]}
    assert routed["compare_selection"]["input_model"].endswith(
        "ModelSelectionCompareCommand"
    )
    assert routed["selection_verdict"]["output_model"].endswith("ModelSelectionVerdict")
    for operation in ("compare_selection", "selection_verdict"):
        module, name = (
            routed[operation]["handler"]["module"],
            routed[operation]["handler"]["name"],
        )
        imported = __import__(module, fromlist=[name])
        assert hasattr(imported, name)


def test_compare_selection_deadlines_are_overlay_keys_and_the_minimum_a_policy_key() -> (
    None
):
    example = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelCodeGraphOverlay.model_validate(example).compare is not None
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    assert policy["graph_min_valid_comparisons"] >= 1
    assert policy["graph_compared_stage"] == 0
    assert "lookup_deadline_seconds" not in policy


# -- the chain, as the runtime binds it --------------------------------------------------


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, compare: bool) -> str:
    clones = tmp_path / "clones"
    head = make_clone(
        clones,
        "example_core",
        {**CORE, "tests/test_models.py": "from example_core.models import Thing\n"},
    )
    make_clone(clones, "example_app", {"pyproject.toml": "[project]\nname='x'\n"})
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not compare:
        del document["compare"]
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
    runtime_code_graph._VERSIONS.clear()
    return head


def test_compare_selection_golden_chain_through_the_bound_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _install(tmp_path, monkeypatch, compare=True)
    command = _command(
        repo="example_core",
        base_sha=head,
        changed_files=("src/example_core/models.py",),
        parse=ModelParseSelection(
            stage=0,
            selected_paths=("tests/",),
            selector_version="1.0.0",
            policy_version="p1",
        ),
        test_files=("tests/test_models.py", "tests/test_other.py"),
    )
    result = HandlerCodeGraphCompareSelectionRequested().handle(command)
    row = result.row
    assert result.ledger == "not-written"
    assert row.outcome is EnumCompareOutcome.COMPARED
    assert row.graph_source is EnumGraphAnswerSource.FILE_STORE
    assert row.fallback_reason is EnumLookupFallbackReason.BOLT_URI_UNRESOLVED
    assert row.indexed_commit == head
    assert (row.graph_only, row.parse_only, row.both) == (0, 1, 1)
    assert not row.valid_for_verdict


def test_compare_selection_error_chain_an_overlay_without_a_compare_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, compare=False)
    with pytest.raises(ValueError, match="no compare block"):
        HandlerCodeGraphCompareSelectionRequested().handle(_command())


def test_selection_verdict_golden_chain_through_the_bound_handler() -> None:
    request = ModelSelectionVerdictRequest(
        rows=(_row(1),),
        window_start=NOW - timedelta(days=8),
        now=NOW,
        graph_min_valid_comparisons=1,
        nightly_nights_observed=7,
    )
    verdict = HandlerCodeGraphSelectionVerdictRequested().handle(request)
    assert verdict.part_one is EnumPartOneVerdict.GRAPH_FOUND_NOTHING_EXTRA
    assert verdict.part_two is EnumPartTwoVerdict.NOT_APPLICABLE
