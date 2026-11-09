# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The piece 11 reading run: the row it writes, each failing control and the lab fact sources (OMN-22958)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import (
    GitRuntimeMergeSource,
    LabDevDeployFacts,
    LaneHealthReceiptSource,
    RuntimeProbeSource,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    glob_regex,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployReadingCommand,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_dev_deploy_reading_effect.dev_deploy_effect_support import (
    HOST,
    LANE,
    RUN_AT,
    SHA_A,
    SHA_B,
    SHA_DRILL,
    at,
    document,
    good_facts,
    handler,
    overlay,
    source,
)

pytestmark = pytest.mark.unit
WRITE = ModelDevDeployReadingCommand(correlation_id="c-1", write=True)
PROBE = ModelDevDeployReadingCommand(correlation_id="c-1", write=False)


def test_good_facts_read_working_and_write_one_row() -> None:
    ledger = InMemoryLedger()
    result = handler(source(), ledger).handle(WRITE)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert (result.merges_checked, result.merges_receipted) == (1, 1)
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    for cell in (
        "lane=dev-deploy-reading",
        "src=dev-deploy-reading",
        "piece=11",
        "drill=no",
        "verdict=WORKING",
        "outcome=read",
        f"host={HOST}",
    ):
        assert cell in row, cell


def test_probe_run_appends_nothing() -> None:
    ledger = InMemoryLedger()
    result = handler(source(), ledger).handle(PROBE)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert ledger.lines == []


def test_writing_run_without_an_appender_fails_and_names_why() -> None:
    result = handler(source(), None).handle(WRITE)
    assert result.status == "failed"
    assert result.failure == "no-appender-for-a-writing-run"


def test_a_dropped_receipt_reads_not_working_with_the_merge_named() -> None:
    facts = good_facts().model_copy(update={"receipts": ()})
    ledger = InMemoryLedger()
    result = handler(source(facts), ledger).handle(WRITE)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0].startswith(f"no-pass-receipt:{SHA_A[:12]}")
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_a_probe_that_saw_another_sha_reads_not_working() -> None:
    probe = good_facts().probes[0].model_copy(update={"observed_sha": SHA_B})
    result = handler(
        source(good_facts().model_copy(update={"probes": (probe,)})), None
    ).handle(PROBE)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(r.startswith("probe-sha-mismatch:") for r in result.reasons)


def test_a_stale_probe_reads_not_working() -> None:
    probe = (
        good_facts()
        .probes[0]
        .model_copy(update={"probed_at": at("11:00").replace(day=6)})
    )
    result = handler(
        source(good_facts().model_copy(update={"probes": (probe,)})), None
    ).handle(PROBE)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(r.startswith("probe-stale:") for r in result.reasons)


def test_an_unreadable_receipt_source_reads_unknown_with_source_unreadable() -> None:
    facts = good_facts().model_copy(
        update={
            "receipts": (),
            "receipts_unread_reason": "lane-health projection unreadable: URLError",
        }
    )
    ledger = InMemoryLedger()
    result = handler(source(facts), ledger).handle(WRITE)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert "outcome=source-unreadable" in ledger.lines[0]


def test_an_unreadable_source_never_hides_a_readable_failure() -> None:
    facts = good_facts().model_copy(
        update={
            "receipts": (),
            "probes": (),
            "probes_unread_reason": "runtime probe unreadable: OSError",
        }
    )
    result = handler(source(facts), None).handle(PROBE)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING


def test_drill_run_adds_an_unreceipted_merge_and_tags_the_row() -> None:
    ledger = InMemoryLedger()
    result = handler(source(), ledger, drill=True).handle(WRITE)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0].startswith(f"no-pass-receipt:{SHA_DRILL[:12]}")
    assert "drill=yes" in ledger.lines[0]
    assert "tag=drill" in ledger.lines[0]


def test_the_same_run_time_gives_the_same_row_key_and_a_changed_threshold_a_new_one() -> (
    None
):
    first = handler(source(), None).handle(PROBE).row_key
    again = handler(source(), None).handle(PROBE).row_key
    other = (
        handler(
            source(),
            None,
            overlay(
                thresholds={
                    **document()["thresholds"],
                    "receipt_deadline_seconds": 1800,
                }
            ),
        )
        .handle(PROBE)
        .row_key
    )
    assert first == again
    assert first != other


def test_a_read_run_never_applies_the_drill_fault() -> None:
    ledger = InMemoryLedger()
    result = handler(source(), ledger).handle(WRITE)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert "drill=no" in ledger.lines[0]


def test_overlay_refuses_a_missing_drill_fault() -> None:
    bad = document()
    del bad["drill_fault"]
    with pytest.raises(ValidationError):
        ModelDevDeployReadingOverlay.model_validate(bad)


def test_overlay_has_no_default_for_a_threshold() -> None:
    bad = document()
    del bad["thresholds"]["probe_max_age_seconds"]
    with pytest.raises(ValidationError):
        overlay(thresholds=bad["thresholds"])


# -- the lab fact sources ---------------------------------------------------------------------


def _git_out(*commits: tuple[str, str, list[str]]) -> bytes:
    return "".join(
        f"\x01{sha}\t{stamp}\n" + "".join(f"{f}\n" for f in files) + "\n"
        for sha, stamp, files in commits
    ).encode()


class _Run:
    """A subprocess runner that records every argv and answers one fixed output."""

    def __init__(self, stdout: bytes, code: int = 0) -> None:
        self.seen: list[list[str]] = []
        self._stdout = stdout
        self._code = code

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self._code, self._stdout, b"")


def _run(stdout: bytes, code: int = 0) -> _Run:
    return _Run(stdout, code)


def test_glob_regex_crosses_directories_only_with_double_star() -> None:
    assert glob_regex("src/**").fullmatch("src/a/b/c.py")
    assert glob_regex("docker/*.yml").fullmatch("docker/x.yml")
    assert not glob_regex("docker/*.yml").fullmatch("docker/sub/x.yml")


def test_git_source_marks_a_commit_runtime_affecting_by_its_changed_files() -> None:
    run = _run(
        _git_out(
            (SHA_A, "2026-11-08T09:00:00+00:00", ["src/pkg/a.py", "README.md"]),
            (SHA_B, "2026-11-08T10:30:00-05:00", ["docs/x.md"]),
        )
    )
    merges, error = GitRuntimeMergeSource(
        overlay().merges, {"TEST_CLONE": "/clone"}, run
    ).read(RUN_AT)
    assert error is None
    assert [(m.sha, m.runtime_affecting) for m in merges] == [
        (SHA_A, True),
        (SHA_B, False),
    ]
    assert merges[1].merged_at.utcoffset() == timedelta(0)
    assert "-m" in run.seen[0]
    assert "--first-parent" in run.seen[0]


def test_git_source_names_a_missing_clone_variable_and_a_git_failure() -> None:
    merges, error = GitRuntimeMergeSource(overlay().merges, {}, _run(b"")).read(RUN_AT)
    assert merges == ()
    assert error == "TEST_CLONE is not set"
    merges, error = GitRuntimeMergeSource(
        overlay().merges, {"TEST_CLONE": "/c"}, _run(b"", 128)
    ).read(RUN_AT)
    assert merges == ()
    assert error == "git log exited 128"


def _serve(text: str) -> Any:
    def fetch(_url: str, _timeout: int) -> str:
        return text

    return fetch


def _projection(**row: Any) -> str:
    base = {
        "lane": LANE,
        "receipt_sha": SHA_A,
        "receipt_result": "PASS",
        "receipt_observed_at": "2026-11-08T09:30:00+00:00",
    }
    return json.dumps({"rows": [{**base, **row}]})


def test_receipt_source_reads_the_lanes_receipt_and_ignores_other_lanes() -> None:
    settings = overlay().receipts
    found, error = LaneHealthReceiptSource(settings, LANE, _serve(_projection())).read()
    assert error is None
    assert [(r.sha, r.result) for r in found] == [(SHA_A, "PASS")]
    found, error = LaneHealthReceiptSource(
        settings, LANE, _serve(_projection(lane="other"))
    ).read()
    assert found == ()
    assert error is None


def test_receipt_source_names_an_unreadable_projection_and_a_malformed_row() -> None:
    settings = overlay().receipts

    def down(url: str, timeout: int) -> str:
        raise OSError("refused")

    assert (
        LaneHealthReceiptSource(settings, LANE, down).read()[1]
        == "lane-health projection unreadable: OSError"
    )
    _, error = LaneHealthReceiptSource(
        settings, LANE, _serve(_projection(receipt_sha="abc"))
    ).read()
    assert error is not None
    assert "not a PASS or FAIL receipt" in error


def _health(healthy: bool = True) -> Any:
    return _serve(json.dumps({"details": {"healthy": healthy}}))


def test_probe_source_reads_health_and_the_recorded_build_commit() -> None:
    run = _run(json.dumps({"vcs_ref": SHA_A}).encode())
    probes, error = RuntimeProbeSource(
        overlay().probe, {"TEST_SSH_HOST": "rt"}, _health(), run
    ).read(RUN_AT, SHA_A)
    assert error is None
    assert (probes[0].expected_sha, probes[0].observed_sha, probes[0].ok) == (
        SHA_A,
        SHA_A,
        True,
    )
    assert "rt" in run.seen[0]


def test_probe_source_names_an_unset_host_a_down_runtime_and_a_short_commit() -> None:
    settings = overlay().probe
    _, error = RuntimeProbeSource(settings, {}, _health(), _run(b"{}")).read(
        RUN_AT, SHA_A
    )
    assert error == "runtime probe unreadable: ValueError"
    _, error = RuntimeProbeSource(
        settings,
        {"TEST_SSH_HOST": "rt"},
        _health(),
        _run(json.dumps({"vcs_ref": "abc"}).encode()),
    ).read(RUN_AT, SHA_A)
    assert error is not None
    assert "exact sha" in error


def test_lab_facts_expect_the_newest_settled_runtime_merge_and_report_an_unreadable_merge_list() -> (
    None
):
    run = _run(_git_out((SHA_A, "2026-11-08T09:00:00+00:00", ["src/a.py"])))

    def route(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "git":
            return run(argv, stdin, timeout)
        return subprocess.CompletedProcess(
            list(argv), 0, json.dumps({"vcs_ref": SHA_A}).encode(), b""
        )

    def fetch(url: str, timeout: int) -> str:
        return (
            _projection()
            if "rows" in url
            else json.dumps({"details": {"healthy": True}})
        )

    facts = LabDevDeployFacts(
        overlay(), {"TEST_CLONE": "/c", "TEST_SSH_HOST": "rt"}, run=route, fetch=fetch
    ).read(RUN_AT)
    assert facts.probes[0].expected_sha == SHA_A
    assert facts.receipts_unread_reason is None
    assert facts.probes_unread_reason is None
    broken = LabDevDeployFacts(overlay(), {}, run=route, fetch=fetch).read(RUN_AT)
    assert broken.receipts_unread_reason is not None
    assert broken.probes_unread_reason is not None
