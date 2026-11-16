# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Item identity, producer sequence and barrier fields, as the capture hooks call them.

The hooks load the handler module by file path, so the module-level import set
is pinned here; the node's pydantic surface is exercised through ``handle``.
"""

from __future__ import annotations

import ast
import importlib.util
import multiprocessing
import multiprocessing.pool as multiprocessing_pool
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_capture_item_identity_effect.handlers import (
    HandlerCaptureItemIdentity,
)
from omnimarket.nodes.node_capture_item_identity_effect.models import (
    EnumCaptureItemOperation,
    ModelCaptureItemIdentityRequest,
    ModelCaptureItemIdentityResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_capture_item_identity_effect"
)
HANDLER_FILE = NODE / "handlers" / "handler_item_identity.py"
SESSION = "session-ta21-1"

#: The producer's namespace as omniclaude derives lineage event ids with it.
PRODUCER_NAMESPACE = uuid.UUID("9449178f-fb63-5b0d-b17d-379aafbcfe67")


def _load_by_path() -> ModuleType:
    """Load the handler module the way a hook does: by file path, no package."""
    spec = importlib.util.spec_from_file_location("item_identity_by_path", HANDLER_FILE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["item_identity_by_path"] = module
    spec.loader.exec_module(module)
    return module


ident = _load_by_path()


def test_module_level_imports_only_the_standard_library() -> None:
    """A hook cannot pay a package import; the models load inside ``handle``."""
    tree = ast.parse(HANDLER_FILE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.If):
            assert ast.unparse(node.test) == "TYPE_CHECKING"
    assert imported <= set(sys.stdlib_module_names)


def test_namespace_is_the_producers_and_ids_are_uuid5() -> None:
    assert ident.HOOK_CAPTURE_NAMESPACE == PRODUCER_NAMESPACE
    event_id = uuid.uuid4()
    assert ident.content_record_id(event_id, "prompt") == uuid.uuid5(
        PRODUCER_NAMESPACE, f"{event_id}|prompt"
    )
    assert ident.content_record_id(event_id, "prompt") != ident.content_record_id(
        event_id, "tool_input"
    )


def test_handler_answers_each_operation(tmp_path: Path) -> None:
    handler = HandlerCaptureItemIdentity()
    event_id = uuid.uuid4()
    common = {"item_dir": str(tmp_path), "session_id": SESSION}

    def ask(**fields: Any) -> ModelCaptureItemIdentityResult:
        return handler.handle(ModelCaptureItemIdentityRequest(**common, **fields))

    got = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.CONTENT_RECORD_ID,
            event_id=event_id,
            field="prompt",
        )
    )
    assert got.content_record_id == ident.content_record_id(event_id, "prompt")
    claimed = ask(
        operation=EnumCaptureItemOperation.CLAIM_EVENT_ID,
        hook_event_name="PostToolUse",
        discriminator="toolu-1",
        stream=ident.STREAM_LINEAGE,
        minted_event_id=event_id,
    )
    assert claimed.event_id == event_id
    seq = ask(
        operation=EnumCaptureItemOperation.NEXT_PRODUCER_SEQ,
        stream=ident.STREAM_CONTENT,
    )
    assert seq.sequence == 1
    top = ask(
        operation=EnumCaptureItemOperation.HIGH_WATER, stream=ident.STREAM_CONTENT
    )
    assert top.sequence == 1


def test_handler_refuses_a_request_missing_what_its_operation_reads() -> None:
    handler = HandlerCaptureItemIdentity()
    with pytest.raises(ValueError, match="needs session_id and item_dir"):
        handler.handle(
            ModelCaptureItemIdentityRequest(
                operation=EnumCaptureItemOperation.HIGH_WATER
            )
        )
    with pytest.raises(ValueError, match="content_record_id needs event_id and field"):
        handler.handle(
            ModelCaptureItemIdentityRequest(
                operation=EnumCaptureItemOperation.CONTENT_RECORD_ID
            )
        )


def test_request_model_rejects_an_unknown_operation_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelCaptureItemIdentityRequest.model_validate({"operation": "mint"})
    with pytest.raises(ValidationError):
        ModelCaptureItemIdentityRequest.model_validate(
            {"operation": "high_water", "surprise": 1}
        )


def test_partner_adopts_the_first_hooks_event_id_in_either_order(
    tmp_path: Path,
) -> None:
    for first, second in (
        (ident.STREAM_LINEAGE, ident.STREAM_CONTENT),
        (ident.STREAM_CONTENT, ident.STREAM_LINEAGE),
    ):
        item_dir = tmp_path / f"items-{first}"
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        kwargs: dict[str, Any] = {
            "session_id": SESSION,
            "agent_id": None,
            "hook_event_name": "PostToolUse",
            "discriminator": "toolu-1",
        }
        got_first = ident.claim_event_id(item_dir, stream=first, minted=mine, **kwargs)
        got_second = ident.claim_event_id(
            item_dir, stream=second, minted=theirs, **kwargs
        )
        assert got_first == got_second == mine


def test_one_stream_arriving_twice_opens_a_second_entry(tmp_path: Path) -> None:
    kwargs: dict[str, Any] = {
        "session_id": SESSION,
        "agent_id": None,
        "hook_event_name": "UserPromptSubmit",
        "discriminator": "same-text",
        "stream": ident.STREAM_LINEAGE,
    }
    first, second = uuid.uuid4(), uuid.uuid4()
    assert ident.claim_event_id(tmp_path, minted=first, **kwargs) == first
    assert ident.claim_event_id(tmp_path, minted=second, **kwargs) == second


def test_a_stale_entry_is_never_matched(tmp_path: Path) -> None:
    kwargs: dict[str, Any] = {
        "session_id": SESSION,
        "agent_id": None,
        "hook_event_name": "PostToolUse",
        "discriminator": "toolu-old",
    }
    old, new = uuid.uuid4(), uuid.uuid4()
    ident.claim_event_id(
        tmp_path, stream=ident.STREAM_LINEAGE, minted=old, now=1000.0, **kwargs
    )
    got = ident.claim_event_id(
        tmp_path,
        stream=ident.STREAM_CONTENT,
        minted=new,
        now=1000.0 + ident.ENTRY_TTL_SECONDS + 1,
        **kwargs,
    )
    assert got == new


def test_an_unknown_stream_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        ident.next_producer_seq(tmp_path, SESSION, "side")


def test_discriminator_is_the_tool_use_id_or_the_text_digest() -> None:
    assert (
        ident.item_discriminator("PostToolUse", {"tool_use_id": "toolu-9"}) == "toolu-9"
    )
    assert ident.item_discriminator(
        "UserPromptSubmit", {"prompt": "hi"}
    ) == ident.sha256_text("hi")
    assert ident.item_discriminator("SessionStart", {"prompt": "hi"}) == ""


#: Spawn workers import this module and start staggered, so a writer can only be
#: known to run beside the others if it waits for them. The barrier wait is the
#: proof; both bounds sit under the 300 s per-test limit the CI split runs with.
_WRITER_BARRIER_WAIT_S = 120.0
_WRITER_RESULT_WAIT_S = 180.0
#: ``Pool.terminate()`` and ``Pool.join()`` take no timeout, so a worker that
#: ignores SIGTERM would hold the pytest process until the per-test thread timeout
#: ends it with ``os._exit``, which loses the split's coverage data. The cleanup
#: runs under this bound, and the result and cleanup waits together stay under
#: ``_TEST_TIMEOUT_S``.
_POOL_CLEANUP_WAIT_S = 20.0
_TEST_TIMEOUT_S = 240
#: A subprocess that runs one pool wait gets longer than that wait, so a hung
#: child fails inside the subprocess (a non-zero exit with its traceback) and
#: never reaches ``TimeoutExpired``; it still ends under ``_TEST_TIMEOUT_S``.
_SUBPROCESS_WAIT_S = _WRITER_RESULT_WAIT_S + 30.0

#: Set in each spawn worker by ``_init_writer``; the parent never holds a barrier here.
_writer_barrier: Any = None

#: Set only in the parent, only while a writers pool is being started. A spawn
#: worker re-imports this module and sees ``False``; a forked worker copies the
#: parent's memory and sees ``True``.
_PARENT_STATE = {"marked": False}


def _init_writer(barrier: Any) -> None:
    global _writer_barrier
    _writer_barrier = barrier


def _issue(args: tuple[str, str, int, float]) -> tuple[int, str, list[int]]:
    """One writer: wait for every other writer, then issue ``count`` sequence numbers.

    Returns the worker's pid and the start method its own process was created with
    (CPython sets it in the child's bootstrap, whatever the parent's default is), so
    the test can read, from the workers themselves, that they ran side by side and
    were started fresh. A pool with fewer workers than barrier parties breaks
    the barrier after ``barrier_wait_s`` and the job raises.
    """
    directory, stream, count, barrier_wait_s = args
    _writer_barrier.wait(timeout=barrier_wait_s)
    seqs = [
        ident.next_producer_seq(Path(directory), SESSION, stream) for _ in range(count)
    ]
    return os.getpid(), multiprocessing.get_start_method(), seqs


def _child_sees_parent_mark(_: None) -> tuple[bool, str]:
    return _PARENT_STATE["marked"], multiprocessing.get_start_method()


def _writer_context() -> multiprocessing.context.BaseContext:
    """Spawn starts fresh interpreters: forking a multithreaded host may deadlock."""
    return multiprocessing.get_context("spawn")


def _start_writer_pool(workers: int, parties: int) -> multiprocessing_pool.Pool:
    """The one place a writers pool is built, always from ``_writer_context()``."""
    ctx = _writer_context()
    barrier = ctx.Barrier(parties)
    return ctx.Pool(workers, initializer=_init_writer, initargs=(barrier,))


def _close_pool(pool: Any, wait_s: float = _POOL_CLEANUP_WAIT_S) -> None:
    """Terminate and join ``pool``, giving up with ``TimeoutError`` after ``wait_s``.

    The two calls run in a daemon thread because neither takes a timeout; a worker
    that never exits then fails this test inside its own bound and does not leave
    the pytest process to the per-test thread timeout.
    """

    def close() -> None:
        pool.terminate()
        pool.join()

    thread = threading.Thread(target=close, daemon=True)
    thread.start()
    thread.join(wait_s)
    if thread.is_alive():
        raise TimeoutError(f"the writers pool did not shut down within {wait_s} s")


def _run_pool(workers: int, parties: int, func: Any, jobs: list[Any]) -> list[Any]:
    """Run ``jobs`` on a writers pool with the parent mark set while workers start."""
    _PARENT_STATE["marked"] = True
    try:
        pool = _start_writer_pool(workers, parties)
        try:
            return pool.map_async(func, jobs, chunksize=1).get(
                timeout=_WRITER_RESULT_WAIT_S
            )
        finally:
            _close_pool(pool)
    finally:
        _PARENT_STATE["marked"] = False


def _run_writers(
    workers: int, parties: int, jobs: list[tuple[str, str, int, float]]
) -> list[tuple[int, str, list[int]]]:
    return _run_pool(workers, parties, _issue, jobs)


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A loaded runner must not turn this into a lock-timeout test.
    monkeypatch.setenv("ONEX_HOOK_LOCK_WAIT_S", "60")
    jobs = [(str(tmp_path), ident.STREAM_CONTENT, 125, _WRITER_BARRIER_WAIT_S)] * 8
    results = _run_writers(8, 8, jobs)
    # Eight distinct workers each held a job at the barrier together, so the
    # writes below came from eight processes and not one worker draining the queue.
    assert len({pid for pid, _, _ in results}) == 8
    # The workers themselves report how they were started.
    assert {method for _, method, _ in results} == {"spawn"}
    issued = [n for _, _, seqs in results for n in seqs]
    assert sorted(issued) == list(range(1, 1001))
    assert ident.high_water(tmp_path, SESSION, ident.STREAM_CONTENT) == 1000
    assert ident.high_water(tmp_path, SESSION, ident.STREAM_LINEAGE) == 0


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_serial_writers_cannot_satisfy_the_concurrency_barrier(tmp_path: Path) -> None:
    """One worker for eight writers breaks the barrier; the gap-free check alone passes."""
    jobs = [(str(tmp_path), ident.STREAM_CONTENT, 5, 3.0)] * 8
    with pytest.raises(threading.BrokenBarrierError):
        _run_writers(1, 8, jobs)


def _worker_view_of_the_parent_mark() -> list[tuple[bool, str]]:
    """What one pool worker sees of the parent's mark."""
    return _run_pool(1, 1, _child_sees_parent_mark, [None])


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_writer_pool_workers_start_fresh_not_forked() -> None:
    saw = _worker_view_of_the_parent_mark()
    assert saw == [(False, "spawn")]


#: Mark names and calls that let a test pass, or end its run, without proving anything.
_SKIPPING_MARKS = frozenset({"skip", "skipif", "xfail"})
_SKIPPING_CALLS = frozenset({"skip", "xfail", "importorskip"})
_EIGHT_WRITER_TEST = (
    "test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers"
)


def _as_list(marks: Any) -> list[Any]:
    """A ``pytestmark`` value is one mark or a list of marks; read both as a list."""
    return list(marks) if isinstance(marks, (list, tuple)) else [marks]


def _mark_names(fn: Any, module_marks: Any) -> set[str]:
    marks = [*_as_list(getattr(fn, "pytestmark", [])), *_as_list(module_marks)]
    return {mark.name for mark in marks}


def _item_mark_names(item: Any) -> set[str]:
    return {mark.name for mark in item.iter_markers()}


def _skipping_lines(source: str) -> list[str]:
    """Every ``pytest.skip``/``xfail``/``importorskip`` call and ``mark.skip*``/``xfail`` use."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        is_call_name = node.attr in _SKIPPING_CALLS
        is_mark_name = node.attr in _SKIPPING_MARKS
        if is_call_name and isinstance(base, ast.Name) and base.id == "pytest":
            found.append(f"line {node.lineno}: pytest.{node.attr}")
        elif is_mark_name and isinstance(base, ast.Attribute) and base.attr == "mark":
            found.append(f"line {node.lineno}: pytest.mark.{node.attr}")
    return found


def test_the_eight_writer_test_carries_no_skip_or_xfail_marker() -> None:
    """A marker on the writers test would let it pass without proving anything."""
    fn = test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers
    assert not _mark_names(fn, pytestmark) & _SKIPPING_MARKS


def test_the_mark_reader_accepts_a_module_mark_that_is_a_list() -> None:
    """``pytestmark`` may be one mark or a list; a list must not raise or hide a skip."""
    fn = test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers
    assert _mark_names(fn, [pytest.mark.unit]) == {"unit", "timeout"}
    for name in sorted(_SKIPPING_MARKS):
        skipping = getattr(pytest.mark, name)
        assert name in _mark_names(fn, [pytest.mark.unit, skipping])
        assert name in _mark_names(fn, skipping)


def test_the_test_file_calls_no_skip_xfail_or_importorskip() -> None:
    """A ``pytest.skip()`` call would let the run pass with the proof not run."""
    assert _skipping_lines(Path(__file__).read_text()) == []


def test_the_skip_reader_finds_each_way_to_skip() -> None:
    """Positive control for the check above: each form is found when it is present."""
    source = (
        "import pytest\n"
        "pytest.skip('x')\n"
        "pytest.xfail('x')\n"
        "pytest.importorskip('x')\n"
        "@pytest.mark.skip\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "@pytest.mark.xfail\n"
        "def test_a(): pass\n"
    )
    assert len(_skipping_lines(source)) == 6
    assert (
        _skipping_lines("import pytest\n@pytest.mark.unit\ndef test_a(): pass\n") == []
    )


def test_the_collected_eight_writer_item_carries_no_marker_added_by_conftest(
    request: pytest.FixtureRequest,
) -> None:
    """A marker a conftest adds at collection is on the item, not on the function."""
    items = [i for i in request.session.items if i.name == _EIGHT_WRITER_TEST]
    for item in items:
        names = _item_mark_names(item)
        assert not names & _SKIPPING_MARKS, f"{item.nodeid} carries {names}"


class _StubItem:
    name = _EIGHT_WRITER_TEST

    def __init__(self, *names: str) -> None:
        self._marks = [getattr(pytest.mark, name) for name in names]

    def iter_markers(self) -> list[Any]:
        return [mark.mark for mark in self._marks]


def test_the_item_marker_reader_sees_a_skip_a_conftest_would_add() -> None:
    """Positive control: the marker names read from an item include an added skip."""
    assert _item_mark_names(_StubItem("unit", "skip")) >= {"skip"}
    assert not _item_mark_names(_StubItem("unit")) & _SKIPPING_MARKS


def test_a_pool_that_does_not_shut_down_fails_inside_its_bound() -> None:
    """``terminate``/``join`` that never return end in ``TimeoutError``, not a hang."""
    release = threading.Event()

    class Stuck:
        def terminate(self) -> None:
            release.wait()

        def join(self) -> None:
            release.wait()

    try:
        with pytest.raises(TimeoutError):
            _close_pool(Stuck(), wait_s=0.2)
    finally:
        release.set()


#: Each variant runs in a fresh single-threaded interpreter that forks, so the
#: multithreaded pytest process these controls guard is never forked itself.
#: ``set_start_method("spawn")`` makes the default method differ from the forced
#: context: a worker still reports the method that created it, so both tests fail.
_FORK_VARIANT_PRELUDE = """
import multiprocessing
multiprocessing.set_start_method("spawn")
import tests.test_capture_item_identity_effect as mod

mod._writer_context = lambda: multiprocessing.get_context("fork")
"""

_FRESH_WORKER_FORK_VARIANT = (
    _FORK_VARIANT_PRELUDE
    + """
saw = mod._worker_view_of_the_parent_mark()
if saw != [(True, "fork")]:
    raise SystemExit(f"a forked worker was expected to report (True, 'fork'), saw {saw}")
print("fork variant: the worker saw the parent mark, so the fresh-worker test goes red")
"""
)

_WRITERS_FORK_VARIANT = (
    _FORK_VARIANT_PRELUDE
    + """
import pathlib
import tempfile
import traceback
import pytest

with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as tmp:
    try:
        mod.test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers(
            pathlib.Path(tmp), patch
        )
    except AssertionError as error:
        failed_line = traceback.extract_tb(error.__traceback__)[-1].line or ""
        if '== {"spawn"}' not in failed_line:
            raise SystemExit(f"the writers test failed on another line: {failed_line}")
        print("fork variant: the writers test failed on the start method check")
    else:
        raise SystemExit("the writers test passed with a fork context")
"""
)


def _run_fork_variant(script: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
    }
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_WAIT_S,
        check=False,
    )


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_fresh_worker_test_fails_when_the_writer_context_is_fork() -> None:
    """Red-then-green control: the fresh-worker test with ``_writer_context`` forked."""
    done = _run_fork_variant(_FRESH_WORKER_FORK_VARIANT)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.timeout(_TEST_TIMEOUT_S)
def test_writers_test_fails_when_the_writer_context_is_fork_under_a_spawn_default() -> (
    None
):
    """Red-then-green control: the writers test with ``_writer_context`` forked."""
    done = _run_fork_variant(_WRITERS_FORK_VARIANT)
    assert done.returncode == 0, done.stdout + done.stderr


def test_barrier_states_the_high_water_and_the_lineage_capture_flag() -> None:
    lineage = ident.build_barrier_payload(
        session_id=SESSION,
        barrier_id="b-1",
        stream=ident.STREAM_LINEAGE,
        high_water_value=12,
        content_capture_enabled=False,
        emitted_at="2026-10-15T06:00:00+00:00",
    )
    assert lineage["high_water"] == 12
    assert lineage["content_capture"] == "disabled"
    assert lineage["record_type"] == "barrier"
    content = ident.build_barrier_payload(
        session_id=SESSION,
        barrier_id="b-2",
        stream=ident.STREAM_CONTENT,
        high_water_value=5,
        content_capture_enabled=True,
        emitted_at="2026-10-15T06:00:00+00:00",
    )
    assert "content_capture" not in content
    assert content["content_kind"] == "barrier"


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.capture-item-identity-resolve.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.capture-item-identity-resolved.v1"
    ]
    assert contract["node_type"] == "effect"
    assert contract["handler"]["class"] == HandlerCaptureItemIdentity.__name__
    assert HandlerCaptureItemIdentity.__module__ == contract["handler"]["module"]


# ---------------------------------------------------------------------------
# Composite operations: one call per hook (OMN-18531)
# ---------------------------------------------------------------------------

HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
CONTENT_TOPIC = "onex.cmd.omniintelligence.content-captured.v1"


def _lineage_payload(hook: str, field: str, minted: uuid.UUID) -> dict[str, Any]:
    """A mapped hook.event as ``model_dump(mode="json")`` shapes it."""
    ref = {
        "field": field,
        "value": "raw",
        "length": 3,
        "content_record_id": str(ident.content_record_id(minted, field)),
    }
    return {
        "event_id": str(minted),
        "hook_event_name": hook,
        "lineage": {"session_id": SESSION, "agent_id": None},
        "payload": {"hook_event_name": hook, f"{field}_ref": dict(ref)},
        "content_refs": [dict(ref)],
    }


def _lineage_ids(stamped: dict[str, Any], field: str) -> set[str]:
    return {
        stamped["payload"][f"{field}_ref"]["content_record_id"],
        *(r["content_record_id"] for r in stamped["content_refs"]),
    }


@pytest.mark.parametrize("lineage_first", [True, False])
def test_the_two_hooks_join_on_one_record_id_in_either_order(
    tmp_path: Path, lineage_first: bool
) -> None:
    journal = tmp_path / "journal"
    hook_input = {"session_id": SESSION, "tool_use_id": "toolu-7"}
    minted = uuid.uuid4()
    sent = _lineage_payload("PreToolUse", "tool_input", minted)
    records = [
        {"content_kind": "tool_input", "chunk_index": 0},
        {"content_kind": "tool_input", "chunk_index": 1},
    ]

    def lineage() -> dict[str, Any]:
        return ident.stamp_lineage_event(journal, sent, hook_input)

    def content() -> list[dict[str, Any]]:
        return ident.stamp_content_records(
            journal,
            session_id=SESSION,
            agent_id=None,
            hook_input=hook_input,
            records=records,
        )

    if lineage_first:
        stamped, chunks = lineage(), content()
    else:
        chunks, stamped = content(), lineage()
    settled = uuid.UUID(stamped["event_id"])
    assert (settled == minted) is lineage_first
    expected = str(ident.content_record_id(settled, "tool_input"))
    assert _lineage_ids(stamped, "tool_input") == {expected}
    assert {c["content_record_id"] for c in chunks} == {expected}
    # The caller's objects are not changed.
    assert sent["event_id"] == str(minted)
    assert "content_record_id" not in records[0]


def test_each_composite_stamps_its_streams_sequence(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    hook_input = {"session_id": SESSION, "tool_use_id": "toolu-8"}
    first = ident.stamp_lineage_event(
        journal, _lineage_payload("PreToolUse", "tool_input", uuid.uuid4()), hook_input
    )
    second = ident.stamp_lineage_event(
        journal,
        _lineage_payload("PostToolUse", "tool_response", uuid.uuid4()),
        hook_input,
    )
    assert [(p["stream"], p["producer_seq"]) for p in (first, second)] == [
        ("lineage", 1),
        ("lineage", 2),
    ]
    chunks = ident.stamp_content_records(
        journal,
        session_id=SESSION,
        agent_id=None,
        hook_input=hook_input,
        records=[
            {"content_kind": "tool_input"},
            {"content_kind": "tool_response"},
            {"content_kind": "checkpoint"},
        ],
    )
    assert [(c["stream"], c["producer_seq"]) for c in chunks] == [
        ("content", 1),
        ("content", 2),
        ("content", 3),
    ]
    # A kind outside full capture is sequenced but carries no record id.
    assert "content_record_id" not in chunks[2]
    # Each record id is the one its own lineage event's reference carries.
    assert chunks[0]["content_record_id"] in _lineage_ids(first, "tool_input")
    assert chunks[1]["content_record_id"] in _lineage_ids(second, "tool_response")
    item_dir = ident.item_dir_for(journal)
    assert ident.high_water(item_dir, SESSION, ident.STREAM_CONTENT) == 3
    assert ident.next_producer_seqs(item_dir, SESSION, ident.STREAM_CONTENT, 0) == []
    with pytest.raises(ValueError, match="must not be negative"):
        ident.next_producer_seqs(item_dir, SESSION, ident.STREAM_CONTENT, -1)


def test_a_hook_outside_full_capture_keeps_its_minted_id(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    minted = uuid.uuid4()
    sent = _lineage_payload("SessionStart", "source", minted)
    stamped = ident.stamp_lineage_event(journal, sent, {"session_id": SESSION})
    assert stamped["event_id"] == str(minted)
    assert stamped["producer_seq"] == 1
    assert not list(ident.item_dir_for(journal).glob("*.items"))


def test_the_barrier_call_reads_every_streams_high_water(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    item_dir = ident.item_dir_for(journal)
    ident.next_producer_seqs(item_dir, SESSION, ident.STREAM_LINEAGE, 4)
    ident.next_producer_seqs(item_dir, SESSION, ident.STREAM_CONTENT, 9)
    lineage, content = ident.session_barrier_payloads(
        journal,
        session_id=SESSION,
        streams=[ident.STREAM_LINEAGE, ident.STREAM_CONTENT],
        content_capture_enabled=True,
        barrier_id="b-9",
        emitted_at="2026-10-16T13:00:00+00:00",
    )
    assert (lineage["high_water"], content["high_water"]) == (4, 9)
    assert lineage["barrier_id"] == content["barrier_id"] == "b-9"
    assert lineage["content_capture"] == "enabled"
    assert "content_capture" not in content
    # A barrier consumes no sequence number.
    assert ident.high_water(item_dir, SESSION, ident.STREAM_LINEAGE) == 4
    minted = ident.session_barrier_payloads(
        journal,
        session_id=SESSION,
        streams=[ident.STREAM_LINEAGE],
        content_capture_enabled=False,
    )
    assert uuid.UUID(minted[0]["barrier_id"])
    assert minted[0]["content_capture"] == "disabled"
    with pytest.raises(ValueError, match="unknown stream"):
        ident.session_barrier_payloads(
            journal,
            session_id=SESSION,
            streams=["metrics"],
            content_capture_enabled=True,
        )


def test_handler_answers_each_composite_operation(tmp_path: Path) -> None:
    handler = HandlerCaptureItemIdentity()
    journal = str(tmp_path / "journal")
    hook_input: dict[str, Any] = {"session_id": SESSION, "prompt": "hello"}
    minted = uuid.uuid4()
    lineage = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.STAMP_LINEAGE_EVENT,
            journal_dir=journal,
            payload=_lineage_payload("UserPromptSubmit", "prompt", minted),
            hook_input=hook_input,
        )
    )
    assert lineage.payload is not None
    assert lineage.payload["producer_seq"] == 1
    content = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.STAMP_CONTENT_RECORDS,
            journal_dir=journal,
            session_id=SESSION,
            hook_input=hook_input,
            records=[{"content_kind": "prompt", "content": "hello"}],
        )
    )
    assert content.records is not None
    assert content.records[0]["content_record_id"] == str(
        ident.content_record_id(minted, "prompt")
    )
    barrier = handler.handle(
        ModelCaptureItemIdentityRequest(
            operation=EnumCaptureItemOperation.SESSION_BARRIER,
            journal_dir=journal,
            session_id=SESSION,
            streams=["lineage", "content"],
            content_capture_enabled=True,
        )
    )
    assert barrier.records is not None
    assert [b["high_water"] for b in barrier.records] == [1, 1]


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            EnumCaptureItemOperation.STAMP_LINEAGE_EVENT,
            "stamp_lineage_event needs journal_dir and payload",
        ),
        (
            EnumCaptureItemOperation.STAMP_CONTENT_RECORDS,
            "stamp_content_records needs journal_dir, session_id and records",
        ),
        (
            EnumCaptureItemOperation.SESSION_BARRIER,
            "session_barrier needs journal_dir, session_id, streams",
        ),
    ],
)
def test_handler_refuses_a_composite_missing_what_it_reads(
    operation: EnumCaptureItemOperation, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        HandlerCaptureItemIdentity().handle(
            ModelCaptureItemIdentityRequest(operation=operation)
        )


def test_every_stamped_field_passes_the_owner_redaction_verbatim(
    tmp_path: Path,
) -> None:
    """The composites stamp only fields the emit path keeps unhashed."""
    from omnimarket.nodes.node_event_emit_effect.redaction import (
        EnumCaptureClass,
        load_contract,
        redact_capture,
    )

    topics = load_contract().topics
    for name in ident.LINEAGE_STAMP_FIELDS:
        assert topics[HOOK_TOPIC].fields[name] is EnumCaptureClass.CAPTURE_VERBATIM
    for name in ident.CONTENT_STAMP_FIELDS:
        assert topics[CONTENT_TOPIC].fields[name] is EnumCaptureClass.CAPTURE_VERBATIM
    journal = tmp_path / "journal"
    hook_input = {"session_id": SESSION, "prompt": "plain words"}
    (chunk,) = ident.stamp_content_records(
        journal,
        session_id=SESSION,
        agent_id=None,
        hook_input=hook_input,
        records=[
            {
                "session_id": SESSION,
                "content_kind": "prompt",
                "content": "plain words",
                "chunk_index": 0,
                "chunk_count": 1,
            }
        ],
    )
    out = redact_capture(chunk, CONTENT_TOPIC)
    for name in ident.CONTENT_STAMP_FIELDS:
        assert out[name] == chunk[name], name
    lineage = ident.stamp_lineage_event(
        journal,
        {
            "event_id": str(uuid.uuid4()),
            "hook_event_name": "Stop",
            "session_id": SESSION,
            "lineage": {"session_id": SESSION},
        },
        {"session_id": SESSION},
    )
    out = redact_capture(
        {k: lineage[k] for k in ("event_id", "hook_event_name", "session_id")}
        | {name: lineage[name] for name in ident.LINEAGE_STAMP_FIELDS},
        HOOK_TOPIC,
    )
    for name in ident.LINEAGE_STAMP_FIELDS:
        assert out[name] == lineage[name], name
    (barrier,) = ident.session_barrier_payloads(
        journal,
        session_id=SESSION,
        streams=[ident.STREAM_LINEAGE],
        content_capture_enabled=False,
    )
    out = redact_capture(barrier, CONTENT_TOPIC)
    assert out["high_water"] == 1
    assert out["content_capture"] == "disabled"
    # Positive control: a field the contract does not declare is still hashed.
    out = redact_capture({**chunk, "producer_sequence": 7}, CONTENT_TOPIC)
    assert str(out["producer_sequence"]).startswith("sha256:")
