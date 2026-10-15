# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_e2e_bridge_effect: the CI read and the evidence writes of the build-to-lifecycle bridge.

The CI reader returns the shared bridge model, never a ``gh`` subprocess.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest

from omnimarket.models.e2e_bridge import (
    ModelE2eCiReadCompleted,
    ModelE2eCiReadRequest,
    ModelE2eEvidenceFile,
    ModelE2eEvidenceWriteRequest,
    ModelE2eEvidenceWritten,
    ModelE2ePrCiReading,
)
from omnimarket.nodes.node_e2e_bridge_effect.handlers.handler_e2e_bridge import (
    HandlerE2eBridgeEffect,
)

pytestmark = pytest.mark.unit

CID = UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#42"


class FakeReader:
    """Answers each PR's CI read with a canned shared reading."""

    def __init__(self, results: dict[int, ModelE2ePrCiReading | Exception]) -> None:
        self.results = results
        self.requests: list[tuple[str, str, int, UUID]] = []

    async def read_ci(
        self, *, pr_ref: str, repo: str, pr_number: int, correlation_id: UUID
    ) -> ModelE2ePrCiReading:
        self.requests.append((pr_ref, repo, pr_number, correlation_id))
        got = self.results[pr_number]
        if isinstance(got, Exception):
            raise got
        return got.model_copy(update={"pr_ref": pr_ref})


def _checks(
    pr: int,
    overall: Literal["green", "red", "pending"],
    *,
    total: int = 3,
    passed: int = 3,
    failed: int = 0,
) -> ModelE2ePrCiReading:
    return ModelE2ePrCiReading(
        pr_ref=f"OmniNode-ai/omnimarket#{pr}",
        overall=overall,
        total=total,
        passed=passed,
        failed=failed,
        pending=total - passed - failed,
        failing_contexts=("lint",) if failed else (),
    )


def _request(*refs: str, **over: Any) -> ModelE2eCiReadRequest:
    base: dict[str, Any] = {
        "correlation_id": CID,
        "pr_refs": refs,
        "attempt": 0,
        "deadline_at": NOW + timedelta(seconds=1500),
        "timeout_seconds": 1500,
        "interval_seconds": 30,
        "dry_run": False,
    }
    base.update(over)
    return ModelE2eCiReadRequest(**base)


def _handler(
    reader: FakeReader, tmp_path: Path, slept: list[float] | None = None
) -> HandlerE2eBridgeEffect:
    async def _sleep(seconds: float) -> None:
        if slept is not None:
            slept.append(seconds)

    return HandlerE2eBridgeEffect(
        reader=reader, state_root=tmp_path, now=lambda: NOW, sleep=_sleep
    )


class TestCiRead:
    async def test_reads_each_pr_with_its_ref_repo_number_and_correlation_id(
        self, tmp_path: Path
    ) -> None:
        reader = FakeReader(
            {42: _checks(42, "green"), 7: _checks(7, "pending", passed=1)}
        )
        out = await _handler(reader, tmp_path).handle(
            _request(PR, "OmniNode-ai/omnibase_core#7")
        )

        assert reader.requests == [
            (PR, "OmniNode-ai/omnimarket", 42, CID),
            ("OmniNode-ai/omnibase_core#7", "OmniNode-ai/omnibase_core", 7, CID),
        ]
        assert isinstance(out, ModelE2eCiReadCompleted)
        assert out.correlation_id == CID
        assert out.read_at == NOW
        assert [(r.pr_ref, r.overall) for r in out.readings] == [
            (PR, "green"),
            ("OmniNode-ai/omnibase_core#7", "pending"),
        ]
        assert out.readings[0].total == 3
        assert out.readings[0].passed == 3

    async def test_the_request_context_is_echoed_for_the_orchestrator(
        self, tmp_path: Path
    ) -> None:
        reader = FakeReader({42: _checks(42, "green")})
        out = await _handler(reader, tmp_path).handle(
            _request(PR, attempt=5, dry_run=True)
        )

        assert isinstance(out, ModelE2eCiReadCompleted)
        assert out.attempt == 5
        assert out.dry_run is True
        assert out.deadline_at == NOW + timedelta(seconds=1500)
        assert out.timeout_seconds == 1500
        assert out.interval_seconds == 30
        assert out.pr_refs == (PR,)

    async def test_failing_contexts_ride_the_reading(self, tmp_path: Path) -> None:
        reader = FakeReader({42: _checks(42, "red", passed=2, failed=1)})
        out = await _handler(reader, tmp_path).handle(_request(PR))

        assert isinstance(out, ModelE2eCiReadCompleted)
        assert out.readings[0].overall == "red"
        assert out.readings[0].failing_contexts == ("lint",)

    @pytest.mark.parametrize(
        "pr_ref",
        [
            "not-a-valid-ref",
            "invalid-repo#42",
            "owner/repo/extra#42",
            "owner/repo#0",
            "owner/repo#-1",
            "owner/repo#",
            "owner/repo#abc",
            "owner/repo#42#7",
            "owner/repo#²",
            pytest.param("owner/repo#" + "9" * 5000, id="pr-number-too-long"),
        ],
    )
    async def test_an_unparseable_ref_is_reported_without_a_read(
        self, tmp_path: Path, pr_ref: str
    ) -> None:
        reader = FakeReader({})
        out = await _handler(reader, tmp_path).handle(_request(pr_ref))

        assert isinstance(out, ModelE2eCiReadCompleted)
        assert reader.requests == []
        assert out.readings[0].pr_ref == pr_ref
        assert out.readings[0].overall == "unparseable"
        assert out.readings[0].error == "invalid pr_ref format"

    async def test_whitespace_and_leading_zeros_preserve_the_original_pr_ref(
        self, tmp_path: Path
    ) -> None:
        reader = FakeReader({42: _checks(42, "green")})
        pr_ref = " OmniNode-ai/omnimarket # 0042 "
        out = await _handler(reader, tmp_path).handle(_request(pr_ref))

        assert reader.requests == [(pr_ref, "OmniNode-ai/omnimarket", 42, CID)]
        assert isinstance(out, ModelE2eCiReadCompleted)
        assert out.readings[0].pr_ref == pr_ref
        assert out.readings[0].overall == "green"

    async def test_a_failed_read_is_unreadable_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        reader = FakeReader({42: RuntimeError("quota floor")})
        out = await _handler(reader, tmp_path).handle(_request(PR))

        assert isinstance(out, ModelE2eCiReadCompleted)
        assert out.readings[0].overall == "unreadable"
        assert "quota floor" in (out.readings[0].error or "")

    async def test_waits_until_not_before_then_reads(self, tmp_path: Path) -> None:
        slept: list[float] = []
        reader = FakeReader({42: _checks(42, "green")})
        out = await _handler(reader, tmp_path, slept).handle(
            _request(PR, not_before=NOW + timedelta(seconds=30))
        )

        assert slept == [30.0]
        assert isinstance(out, ModelE2eCiReadCompleted)

    async def test_no_wait_when_not_before_has_passed(self, tmp_path: Path) -> None:
        slept: list[float] = []
        reader = FakeReader({42: _checks(42, "green")})
        await _handler(reader, tmp_path, slept).handle(
            _request(PR, not_before=NOW - timedelta(seconds=30))
        )

        assert slept == []

    async def test_never_shells_out_to_gh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: object, **k: object) -> None:
            raise AssertionError("subprocess is not the CI read")

        monkeypatch.setattr(subprocess, "run", _boom)
        monkeypatch.setattr(subprocess, "Popen", _boom)
        reader = FakeReader({42: _checks(42, "green")})
        out = await _handler(reader, tmp_path).handle(_request(PR))

        assert isinstance(out, ModelE2eCiReadCompleted)


class TestEvidenceWrite:
    async def test_writes_each_file_under_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        req = ModelE2eEvidenceWriteRequest(
            correlation_id=CID,
            files=(
                ModelE2eEvidenceFile(filename="pr_refs.json", data={"pr_refs": [PR]}),
                ModelE2eEvidenceFile(filename="receipts.json", data={"prs_merged": 2}),
            ),
        )
        out = await _handler(FakeReader({}), tmp_path).handle(req)

        run_dir = tmp_path / "e2e-runs" / str(CID)
        assert json.loads((run_dir / "pr_refs.json").read_text())["pr_refs"] == [PR]
        receipts = json.loads((run_dir / "receipts.json").read_text())
        assert receipts["prs_merged"] == 2
        assert receipts["written_at"] == NOW.isoformat()
        assert isinstance(out, ModelE2eEvidenceWritten)
        assert out.filenames == ("pr_refs.json", "receipts.json")
        assert out.correlation_id == CID

    async def test_a_filename_that_leaves_the_run_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="plain file name"):
            ModelE2eEvidenceFile(filename="../escape.json", data={})
        with pytest.raises(ValueError, match="plain file name"):
            ModelE2eEvidenceFile(filename="sub/dir.json", data={})
        assert not (tmp_path / "e2e-runs").exists()

    async def test_a_later_write_replaces_an_earlier_one(self, tmp_path: Path) -> None:
        h = _handler(FakeReader({}), tmp_path)
        for n in (1, 2):
            await h.handle(
                ModelE2eEvidenceWriteRequest(
                    correlation_id=CID,
                    files=(
                        ModelE2eEvidenceFile(filename="ci_checks.json", data={"n": n}),
                    ),
                )
            )

        got = json.loads(
            (tmp_path / "e2e-runs" / str(CID) / "ci_checks.json").read_text()
        )
        assert got["n"] == 2

    async def test_state_root_comes_from_the_declared_env_when_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
        h = HandlerE2eBridgeEffect(reader=FakeReader({}), now=lambda: NOW)
        await h.handle(
            ModelE2eEvidenceWriteRequest(
                correlation_id=CID,
                files=(ModelE2eEvidenceFile(filename="a.json", data={}),),
            )
        )

        assert (tmp_path / "e2e-runs" / str(CID) / "a.json").exists()
