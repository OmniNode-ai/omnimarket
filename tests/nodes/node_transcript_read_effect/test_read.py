# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_read_effect reads only under ONEX_CONTEXT_REPLAY_ROOT, which has no default (OMN-21129, TC.6)."""

from __future__ import annotations

import hashlib
import importlib
import pathlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.transcript_parse import (
    EnumTranscriptReadStatus,
    ModelTranscriptParseRequest,
    ModelTranscriptReadRequest,
)
from omnimarket.nodes.node_transcript_parse_compute.handlers.handler_transcript_parse import (
    HandlerTranscriptParse,
)
from omnimarket.nodes.node_transcript_read_effect.handlers.handler_transcript_read import (
    REPLAY_ROOT_ENV,
    HandlerTranscriptRead,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / "node_transcript_read_effect"
LINE = '{"type": "user", "uuid": "u-1", "promptSource": "terminal", "message": {"content": "hi"}}\n'


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "replay"
    (root / "case-1").mkdir(parents=True)
    inside = root / "case-1" / "session.jsonl"
    inside.write_text(LINE, encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(LINE, encoding="utf-8")
    (root / "case-1" / "escape.jsonl").symlink_to(outside)
    (root / "case-1" / "bad.jsonl").write_bytes(b"\xff\xfe")
    return {"root": root, "inside": inside, "outside": outside}


class ReadSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[Path] = []
        real = pathlib.Path.read_bytes

        def spy(path: Path) -> bytes:
            self.paths.append(path)
            return real(path)

        monkeypatch.setattr(pathlib.Path, "read_bytes", spy)


def _read(root: Path | str | None, relative: str) -> Any:
    environ = {} if root is None else {REPLAY_ROOT_ENV: str(root)}
    return HandlerTranscriptRead(environ=environ).handle(
        ModelTranscriptReadRequest(relative_path=relative)
    )


def test_a_file_under_the_root_is_read_with_its_hash(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = ReadSpy(monkeypatch)
    result = _read(tree["root"], "case-1/session.jsonl")
    assert result.status is EnumTranscriptReadStatus.READ
    assert result.relative_path == "case-1/session.jsonl"
    assert result.transcript_text == LINE
    assert result.byte_count == len(LINE.encode())
    assert result.sha256 == hashlib.sha256(LINE.encode()).hexdigest()
    assert spy.paths == [tree["inside"].resolve()]
    parsed = HandlerTranscriptParse().handle(
        ModelTranscriptParseRequest(transcript_text=result.transcript_text)
    )
    assert parsed.operator_typed_count == 1


@pytest.mark.parametrize("root", [None, "", "   "])
def test_an_unset_root_is_refused_with_no_default(
    root: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: no default path; nothing is read when the variable is unset or empty."""
    spy = ReadSpy(monkeypatch)
    monkeypatch.chdir(REPO)
    result = _read(root, "case-1/session.jsonl")
    assert result.status is EnumTranscriptReadStatus.REFUSED_ROOT_UNSET
    assert result.transcript_text is None
    assert spy.paths == []


def test_the_default_handler_reads_the_process_environment(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REPLAY_ROOT_ENV, raising=False)
    request = ModelTranscriptReadRequest(relative_path="case-1/session.jsonl")
    unset = HandlerTranscriptRead().handle(request)
    assert unset.status is EnumTranscriptReadStatus.REFUSED_ROOT_UNSET
    monkeypatch.setenv(REPLAY_ROOT_ENV, str(tree["root"]))
    assert (
        HandlerTranscriptRead().handle(request).status is EnumTranscriptReadStatus.READ
    )


@pytest.mark.parametrize(
    "relative",
    ["../outside.jsonl", "case-1/../../outside.jsonl", "case-1/escape.jsonl", "."],
)
def test_a_path_resolving_outside_the_root_is_refused_before_any_read(
    tree: dict[str, Path], relative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a file read elsewhere is refused, whether by .., a symlink or the root itself."""
    spy = ReadSpy(monkeypatch)
    result = _read(tree["root"], relative)
    assert result.status is EnumTranscriptReadStatus.REFUSED_OUTSIDE_ROOT
    assert result.relative_path == relative
    assert result.transcript_text is None
    assert spy.paths == []


def test_an_absolute_path_is_refused_even_inside_the_root(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = ReadSpy(monkeypatch)
    result = _read(tree["root"], str(tree["inside"]))
    assert result.status is EnumTranscriptReadStatus.REFUSED_OUTSIDE_ROOT
    assert spy.paths == []


def test_missing_and_undecodable_files_are_typed_results(tree: dict[str, Path]) -> None:
    assert (
        _read(tree["root"], "case-1/none.jsonl").status
        is EnumTranscriptReadStatus.NOT_FOUND
    )
    assert _read(tree["root"], "case-1").status is EnumTranscriptReadStatus.NOT_FOUND
    assert (
        _read(tree["root"], "case-1/bad.jsonl").status
        is EnumTranscriptReadStatus.DECODE_FAILED
    )


def test_the_contract_declares_topics_handler_and_the_required_root() -> None:
    contract: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    command = "onex.cmd.omnimarket.transcript-read-requested.v1"
    event = "onex.evt.omnimarket.transcript-read-completed.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["runtime_dispatch"]["command_topic"] == command
    assert contract["terminal_event"] == event
    assert [e["name"] for e in contract["required_env"]] == [REPLAY_ROOT_ENV]
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)


def test_the_node_source_names_no_default_path_topic_or_envelope() -> None:
    for path in NODE_DIR.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        for needle in (
            "ModelEventEnvelope",
            "onex.cmd.",
            "onex.evt.",
            "home()",
            "expanduser",
            "~/",
        ):
            assert needle not in body, (path, needle)
