# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler end to end against a fake DreamSheets server (any OS).

The launcher here is a test-only direct launcher: the shipped package has no
unsandboxed path. The sandbox itself is covered by
``test_sandbox_bwrap_linux.py``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dreamsheets_structure_effect import (
    EnumDreamsheetsStructureStatus,
    HandlerDreamsheetsStructureEffect,
    ModelDreamsheetsStructureRequest,
    ModelDreamsheetsStructureResult,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.handlers.handler_dreamsheets_structure_effect import (
    RUN_DIR_PREFIX,
)
from omnimarket.nodes.node_dreamsheets_structure_effect.protocols.sandbox_launcher import (
    RunLayout,
)

pytestmark = pytest.mark.unit

FAKE = Path(__file__).with_name("fake_dreamsheets_server.py")
MARKER = "ZQXMARK7F3A"


@dataclass(frozen=True)
class FakeLauncher:
    """Runs the fake server directly; records what it was given."""

    scenario: str
    seen: list[RunLayout]

    @property
    def binary(self) -> Path:
        return FAKE

    def document_path(self, layout: RunLayout) -> str:
        return str(layout.input_file)

    def command(self, layout: RunLayout) -> list[str]:
        self.seen.append(layout)
        return [sys.executable, str(FAKE), self.scenario]

    def environment(self, layout: RunLayout) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env


def doc(tmp_path: Path, name: str = f"{MARKER}.dsheet") -> Path:
    d = tmp_path / MARKER
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(b"PK fake dsheet " + MARKER.encode())
    return p


def run(
    tmp_path: Path, scenario: str, path: Path | None = None, **kw: Any
) -> tuple[ModelDreamsheetsStructureResult, list[RunLayout]]:
    seen: list[RunLayout] = []
    handler = HandlerDreamsheetsStructureEffect(
        FakeLauncher(scenario, seen), run_base_dir=tmp_path / "runs", **kw
    )
    target = path or doc(tmp_path)
    result = handler.run_sync(
        ModelDreamsheetsStructureRequest(
            document_path=str(target), correlation_id="c-1"
        )
    )
    return result, seen


def test_completed_record_hashes_and_cleanup(tmp_path: Path) -> None:
    source = doc(tmp_path)
    result, seen = run(tmp_path, "ok", source)
    assert result.status is EnumDreamsheetsStructureStatus.COMPLETED
    summary = result.summary
    assert summary is not None
    assert summary.tables == 2
    assert summary.rows is None
    assert result.input_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.binary_sha256 == hashlib.sha256(FAKE.read_bytes()).hexdigest()
    assert result.dreamsheets_version == "0.2.9"
    assert result.correlation_id == "c-1"
    assert not seen[0].root.exists(), "run dir deleted"


def test_values_mode_writes_the_config_and_reads_counts(tmp_path: Path) -> None:
    result, _ = run(tmp_path, "ok_values", values_withheld=False)
    assert result.status is EnumDreamsheetsStructureStatus.COMPLETED
    assert result.summary is not None
    assert result.summary.cells == 12


@pytest.mark.parametrize(
    ("scenario", "kw", "code"),
    [
        ("open_error", {}, "E_OPEN_IO"),
        ("summary_error", {}, "E_DREAMSHEETS_ERROR"),
        ("bad_record", {}, "E_RECORD_INVALID"),
        ("extra_key", {}, "E_RECORD_INVALID"),
        ("ok_values", {}, "E_PRIVACY_MISMATCH"),
        ("ok", {"values_withheld": False}, "E_PRIVACY_MISMATCH"),
        ("garbage", {}, "E_PROTOCOL"),
        ("crash", {}, "E_PROTOCOL"),
        ("flood", {}, "E_OUTPUT_TOO_LARGE"),
        ("sleep", {"timeout_s": 1.0}, "E_TIMEOUT"),
    ],
)
def test_failures_carry_a_code_and_nothing_from_the_document(
    tmp_path: Path, scenario: str, kw: dict[str, Any], code: str
) -> None:
    started = time.monotonic()
    result, seen = run(tmp_path, scenario, **kw)
    assert result.status is EnumDreamsheetsStructureStatus.FAILED
    assert result.error_code == code
    assert result.summary is None
    assert MARKER not in result.model_dump_json()
    assert not seen[0].root.exists()
    assert time.monotonic() - started < 30


@pytest.mark.parametrize(
    ("make", "kw", "code"),
    [
        (lambda _t: Path("relative.dsheet"), {}, "E_INPUT_PATH"),
        (lambda t: doc(t, "x.txt"), {}, "E_INPUT_PATH"),
        (lambda t: t / "missing.dsheet", {}, "E_INPUT_MISSING"),
        (lambda t: doc(t), {"max_input_bytes": 3}, "E_INPUT_TOO_LARGE"),
        (lambda t: doc(t), {"binary_sha256": "0" * 64}, "E_BINARY_HASH"),
    ],
)
def test_refusals_happen_before_anything_runs(
    tmp_path: Path, make: Callable[[Path], Path], kw: dict[str, Any], code: str
) -> None:
    result, seen = run(tmp_path, "ok", make(tmp_path), **kw)
    assert result.status is EnumDreamsheetsStructureStatus.REFUSED
    assert result.error_code == code
    assert MARKER not in result.model_dump_json()
    assert seen == [], "the binary never started"


def test_matching_pin_runs(tmp_path: Path) -> None:
    pin = hashlib.sha256(FAKE.read_bytes()).hexdigest().upper()
    result, _ = run(tmp_path, "ok", binary_sha256=pin)
    assert result.status is EnumDreamsheetsStructureStatus.COMPLETED


def test_stale_run_dirs_are_swept(tmp_path: Path) -> None:
    stale = tmp_path / "runs" / f"{RUN_DIR_PREFIX}old"
    stale.mkdir(parents=True)
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = tmp_path / "runs" / f"{RUN_DIR_PREFIX}live"
    fresh.mkdir()
    run(tmp_path, "ok")
    assert not stale.exists()
    assert fresh.exists()


def test_the_copy_is_read_only_and_the_source_untouched(tmp_path: Path) -> None:
    source = doc(tmp_path)
    before = source.read_bytes()
    seen: list[RunLayout] = []

    class Peek(FakeLauncher):
        def command(self, layout: RunLayout) -> list[str]:
            assert not os.access(layout.input_file, os.W_OK)
            assert layout.input_file.read_bytes() == before
            assert list(layout.config_dir.iterdir()) == [], "strict: empty config"
            return super().command(layout)

    handler = HandlerDreamsheetsStructureEffect(
        Peek("ok", seen), run_base_dir=tmp_path / "runs"
    )
    result = handler.run_sync(
        ModelDreamsheetsStructureRequest(document_path=str(source))
    )
    assert result.status is EnumDreamsheetsStructureStatus.COMPLETED
    assert source.read_bytes() == before
