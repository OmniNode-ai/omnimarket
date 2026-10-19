# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compact package read effect returns an installed compaction verbatim.

Plan task TA.20b: the onex plugin's ``hook_content_capture.py --kind compact``,
run at PostCompact, asks this node for the newest ``compact_boundary`` record of
the session transcript and every record after it, unmodified. The capture
loads the handler module by file path, so its module-level import set is pinned
here too.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from omnimarket.nodes.node_compact_package_read_effect.models import (
    ModelCompactPackageReadResult,
)

pytestmark = pytest.mark.unit

COMPLETED_TOPIC = "onex.evt.omnimarket.compact-package-read-completed.v1"

HANDLER_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_compact_package_read_effect"
    / "handlers"
    / "handler_compact_package_read.py"
)

BOUNDARY_UUID = "783e1d6e-1111-2222-3333-444444444444"
OLD_BOUNDARY_UUID = "00000000-aaaa-bbbb-cccc-000000000001"
REPLACEMENT_UUIDS = (
    "99990000-0000-0000-0000-000000000001",
    "99990000-0000-0000-0000-000000000002",
)


def _load_by_path() -> ModuleType:
    """Load the handler module the way the capture does: by file path, no package."""
    spec = importlib.util.spec_from_file_location(
        "compact_package_read_by_path", HANDLER_FILE
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["compact_package_read_by_path"] = module
    spec.loader.exec_module(module)
    return module


reader = _load_by_path()


def boundary_line(uuid: str) -> str:
    # Spacing and key order a parse-and-reserialise would change.
    return (
        '{"parentUuid": null, "type": "system", "subtype": "compact_boundary", '
        f'"uuid": "{uuid}", "compactMetadata": {{"trigger": "manual", "preTokens": 2712}}}}'
    )


def entry_line(kind: str, uuid: str, text: str) -> str:
    return json.dumps(
        {"type": kind, "uuid": uuid, "message": {"role": kind, "content": text}},
        ensure_ascii=False,
    )


def transcript_lines() -> list[str]:
    return [
        entry_line("user", "11110000-0000-0000-0000-000000000001", "an early prompt"),
        boundary_line(OLD_BOUNDARY_UUID),
        entry_line(
            "assistant", "11110000-0000-0000-0000-000000000003", "a middle reply"
        ),
        boundary_line(BOUNDARY_UUID),
        entry_line("user", REPLACEMENT_UUIDS[0], "summary café — kept"),
        entry_line("assistant", REPLACEMENT_UUIDS[1], "ack"),
    ]


def expected_package(lines: list[str]) -> str:
    start = max(i for i, line in enumerate(lines) if BOUNDARY_UUID in line)
    return "\n".join(lines[start:]) + "\n"


def transcript_file(tmp_path: Path, lines: list[str] | None = None) -> Path:
    path = tmp_path / "projects" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines or transcript_lines()) + "\n", encoding="utf-8")
    return path


def test_module_level_imports_only_the_standard_library() -> None:
    """The capture cannot pay a package import; the models load inside ``handle``."""
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


def test_the_newest_boundary_and_every_record_after_it_are_returned(
    tmp_path: Path,
) -> None:
    package = reader.compact_package(transcript_file(tmp_path))
    assert package == expected_package(transcript_lines())
    assert BOUNDARY_UUID in package
    assert all(uuid in package for uuid in REPLACEMENT_UUIDS)
    assert (
        hashlib.sha256(package.encode()).hexdigest()
        == hashlib.sha256(expected_package(transcript_lines()).encode()).hexdigest()
    )


def test_an_older_boundary_and_the_records_before_the_newest_are_not_returned(
    tmp_path: Path,
) -> None:
    package = reader.compact_package(transcript_file(tmp_path))
    assert OLD_BOUNDARY_UUID not in package
    assert "an early prompt" not in package
    assert "a middle reply" not in package


def test_the_records_ride_unmodified(tmp_path: Path) -> None:
    package = reader.compact_package(transcript_file(tmp_path))
    assert boundary_line(BOUNDARY_UUID) + "\n" in package
    assert "café — kept" in package


def test_a_transcript_with_no_boundary_has_no_package(tmp_path: Path) -> None:
    path = transcript_file(
        tmp_path, [entry_line("user", "11110000-0000-0000-0000-000000000001", "hi")]
    )
    assert reader.compact_package(path) == ""


def test_a_missing_transcript_has_no_package(tmp_path: Path) -> None:
    assert reader.compact_package(tmp_path / "gone.jsonl") == ""


def test_a_boundary_quoted_inside_a_tool_result_is_not_a_boundary(
    tmp_path: Path,
) -> None:
    quoted = entry_line(
        "user",
        "11110000-0000-0000-0000-0000000000aa",
        'grep hit: "subtype": "compact_boundary"',
    )
    assert reader.compact_package(transcript_file(tmp_path, [quoted])) == ""


def test_a_boundary_flushed_after_the_hook_fired_is_found_by_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = transcript_file(tmp_path, transcript_lines()[:1])
    full = transcript_lines()

    def flush(_seconds: float) -> None:
        path.write_text("\n".join(full) + "\n", encoding="utf-8")

    monkeypatch.setattr(reader.time, "sleep", flush)
    package = reader.package_when_flushed(path, attempts=3, pause_s=0.5)
    assert package == expected_package(full)


def test_polling_gives_up_after_the_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(reader.time, "sleep", slept.append)
    assert reader.package_when_flushed(tmp_path / "gone.jsonl", attempts=3) == ""
    assert len(slept) == 2


def test_a_window_that_cuts_the_boundary_off_finds_nothing_not_a_partial(
    tmp_path: Path,
) -> None:
    lines = [boundary_line(BOUNDARY_UUID)] + [
        entry_line("user", f"77770000-0000-0000-0000-{n:012d}", "kept " + "x" * 90)
        for n in range(200)
    ]
    path = transcript_file(tmp_path, lines)
    assert reader.compact_package(path, tail_bytes=10 * 1024 * 1024) == (
        expected_package(lines)
    )
    assert reader.compact_package(path, tail_bytes=1024) == ""


def test_compact_items_wraps_the_package_as_one_compact_item(tmp_path: Path) -> None:
    path = transcript_file(tmp_path)
    for key in ("transcript_path", "transcriptPath"):
        assert reader.compact_items({key: str(path)}) == [
            {"content_kind": "compact", "content": expected_package(transcript_lines())}
        ]


@pytest.mark.parametrize(
    "hook_input", [{}, {"transcript_path": ""}, {"transcript_path": 7}]
)
def test_compact_items_without_a_usable_transcript_path_is_empty(
    hook_input: dict[str, object],
) -> None:
    assert reader.compact_items(hook_input) == []


def test_the_contract_declares_its_terminal_topic_and_typed_outputs() -> None:
    contract = yaml.safe_load(
        (HANDLER_FILE.parents[1] / "contract.yaml").read_text(encoding="utf-8")
    )
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert set(contract["outputs"]) == set(ModelCompactPackageReadResult.model_fields)
