# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The red-facts record (OMN-23832): one record per red head and job, written by the filler.

AC1: one pass over a watcher-state fixture with a red job writes one record per red head and
job carrying the failing step and the failure lines, and a new head of the same pull request
reads ``not_filled`` instead of the old head's record. The failing control is the merge base,
where no record is written (the store stays empty).

The error chain: a record that cannot be written leaves the fill counted and the pass completed;
a log cached before this change gets its record with no GitHub read; a head that moved reads
``not_filled`` and shows none of the old head's text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_red_facts import (
    RedFactsStore,
    answer_red_facts,
    build_record,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_red_facts import (
    EnumRedFactsStatus,
    ModelRedFactsRecord,
)
from tests.nodes.node_job_log_filler_effect.planted import (
    HEAD,
    RED_JOB,
    REPO,
    SUMMARY_JOB,
    overlay,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"
STEP_LOG = (
    "2026-11-11T16:00:00.1234567Z ##[group]Run uv run pytest tests/nodes/x.py\n"
    "2026-11-11T16:00:01.0000000Z collecting\n"
    f"2026-11-11T16:00:02.0000000Z token {TOKEN} in a log line\n"
    "2026-11-11T16:00:03.0000000Z FAILED tests/nodes/x.py::test_y - AssertionError: no\n"
    "2026-11-11T16:00:04.0000000Z ##[endgroup]\n"
    "2026-11-11T16:00:05.0000000Z ##[error]Process completed with exit code 1.\n"
).encode()
NEW_HEAD = "b" * 40


def _handler(tmp_path: Path, transport, *, store=None, **settings):
    state_dir = tmp_path / "planted-state"
    store = store or RedFactsStore(state_dir / "red-facts")
    return (
        HandlerJobLogFiller(
            overlay=overlay(**settings),
            reader_factory=lambda _profile: InMemoryJobLogReader(
                tmp_path / "job-logs",
                ttl_seconds=86400,
                tail_bytes=4096,
                transport_factory=lambda: transport,
            ),
            state_reader=watcher_state,
            record_store=store,
        ),
        store,
    )


def test_a_pass_writes_one_record_per_red_head_and_job(tmp_path: Path) -> None:
    transport = transport_for(RED_JOB, SUMMARY_JOB, bodies={RED_JOB: (200, STEP_LOG)})
    handler, store = _handler(tmp_path, transport)
    result = handler.fill(ModelJobLogFillRequest())
    assert result.status is EnumJobLogFillStatus.COMPLETED
    assert (result.records_written, result.records_failed) == (1, 0)  # red only
    [record] = store.read(REPO, 1, HEAD)
    assert (record.check, record.job_id, record.head_sha) == (
        "Tests Gate",
        RED_JOB,
        HEAD,
    )
    assert record.failing_step == "Run uv run pytest tests/nodes/x.py"
    assert record.digest_kind == "log_lines"
    assert any(
        "FAILED tests/nodes/x.py::test_y" in line for line in record.digest_lines
    )
    assert any("##[error]Process completed" in line for line in record.digest_lines)
    assert record.cache_path == str(tmp_path / "job-logs")
    assert TOKEN not in record.model_dump_json()  # the digest holds no token
    assert "2026-11-11T16" not in record.model_dump_json()  # nor the log's timestamps


def test_a_new_head_reads_not_filled_and_never_the_old_heads_text(
    tmp_path: Path,
) -> None:
    handler, store = _handler(
        tmp_path, transport_for(RED_JOB, SUMMARY_JOB, bodies={RED_JOB: (200, STEP_LOG)})
    )
    handler.fill(ModelJobLogFillRequest())
    found = answer_red_facts(store=store, repo=REPO, pr_number=1, head_sha=HEAD)
    assert found.status is EnumRedFactsStatus.FOUND
    moved = answer_red_facts(
        store=store, repo=REPO, pr_number=1, head_sha=NEW_HEAD, head_age_seconds=30
    )
    assert moved.status is EnumRedFactsStatus.NOT_FILLED
    assert moved.records == ()
    assert moved.head_age_seconds == 30
    assert "test_y" not in moved.model_dump_json()
    assert NEW_HEAD[:10] in (moved.reason or "")


def test_control_at_the_merge_base_no_record_is_written(tmp_path: Path) -> None:
    """The failing control: a pass whose reader fills nothing leaves the store empty and the answer not_filled."""

    class NothingReader:
        cache_path = None

        def read(self, repo, job_id, *, cache_only, tail_bytes=None):
            raise RuntimeError("nothing downloaded")

    store = RedFactsStore(tmp_path / "planted-state" / "red-facts")
    handler = HandlerJobLogFiller(
        overlay=overlay(),
        reader_factory=lambda _p: NothingReader(),
        state_reader=watcher_state,
        record_store=store,
    )
    result = handler.fill(ModelJobLogFillRequest())
    assert result.records_written == 0
    assert (
        answer_red_facts(store=store, repo=REPO, pr_number=1, head_sha=HEAD).status
        is EnumRedFactsStatus.NOT_FILLED
    )


def test_a_log_cached_before_the_change_gets_its_record_with_no_github_read(
    tmp_path: Path,
) -> None:
    transport = transport_for(RED_JOB, SUMMARY_JOB, bodies={RED_JOB: (200, STEP_LOG)})
    handler, store = _handler(tmp_path, transport)
    handler.fill(ModelJobLogFillRequest())
    assert len(transport.sent) == 2
    for path in (tmp_path / "planted-state" / "red-facts").rglob("*.json"):
        path.unlink()  # a record that was never written: the log is cached, the memo holds it
    second = handler.fill(ModelJobLogFillRequest())
    assert (second.filled, second.records_written) == (0, 1)
    assert len(transport.sent) == 2  # rebuilt from the cache, no GitHub read
    assert len(store.read(REPO, 1, HEAD)) == 1


def test_error_a_record_that_cannot_be_written_does_not_fail_the_fill(
    tmp_path: Path,
) -> None:
    class BrokenStore(RedFactsStore):
        def write(self, record: ModelRedFactsRecord) -> Path:
            raise OSError("disk full")

    handler, _ = _handler(
        tmp_path,
        transport_for(RED_JOB, SUMMARY_JOB),
        store=BrokenStore(tmp_path / "x"),
    )
    result = handler.fill(ModelJobLogFillRequest())
    assert result.status is EnumJobLogFillStatus.COMPLETED
    assert (result.filled, result.records_written, result.records_failed) == (2, 0, 1)


def test_the_digest_is_capped_by_the_overlay_and_junit_goes_through_the_pytest_digest() -> (
    None
):
    lines = "".join(
        f"FAILED tests/t.py::case_{i} - boom {'x' * 400}\n" for i in range(30)
    )
    capped = build_record(
        repo=REPO,
        pr_number=1,
        head_sha=HEAD,
        job_name="Tests Gate",
        job_id=RED_JOB,
        text=lines,
        total_bytes=len(lines),
        cache_path=None,
        overlay=overlay(red_facts_max_lines=5, red_facts_line_chars=50),
        now=datetime(2026, 11, 11, tzinfo=UTC),
    )
    assert len(capped.digest_lines) == 5
    assert all(len(line) <= 50 for line in capped.digest_lines)
    assert "case_29" in capped.digest_lines[-1]  # the last lines are kept
    junit = (
        '<?xml version="1.0"?><testsuite tests="1" failures="1"><testcase '
        'classname="tests.t" name="test_a"><failure message="boom" type="AssertionError">'
        "tests/t.py:3: AssertionError</failure></testcase></testsuite>"
    )
    record = build_record(
        repo=REPO,
        pr_number=1,
        head_sha=HEAD,
        job_name="Tests Gate",
        job_id=RED_JOB,
        text=junit,
        total_bytes=len(junit),
        cache_path=None,
        overlay=overlay(),
        now=datetime(2026, 11, 11, tzinfo=UTC),
    )
    assert record.digest_kind == "junit"
    assert any("AssertionError" in line for line in record.digest_lines)


def test_the_store_refuses_a_key_that_escapes_its_root(tmp_path: Path) -> None:
    store = RedFactsStore(tmp_path)
    with pytest.raises(ValueError, match="unsafe red-facts key"):
        store.read("../../etc/passwd", 1, HEAD)
    with pytest.raises(ValueError, match="unsafe red-facts key"):
        store.read(REPO, 1, "../x")
