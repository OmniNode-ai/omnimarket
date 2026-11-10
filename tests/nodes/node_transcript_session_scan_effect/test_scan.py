# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_session_scan_effect: manifest, stream kinds, roots, revisions, no prompt text (OMN-22259, T1)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.work_classification import (
    EnumStreamKind,
    ModelSourceRevision,
)
from omnimarket.nodes.node_transcript_session_facts_compute.handlers.handler_transcript_session_facts import (
    HandlerTranscriptSessionFacts,
)
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
    SourceRevisionShortError,
)
from omnimarket.nodes.node_transcript_session_scan_effect.handlers.handler_transcript_session_scan import (
    HandlerTranscriptSessionScan,
    stream_id_of,
)
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptReadRequest,
    ModelTranscriptScanOverlay,
    ModelTranscriptScanRequest,
    TranscriptReadTooLargeError,
    TranscriptRootUnavailableError,
)
from omnimarket.nodes.node_transcript_session_scan_effect.overlay import (
    load_scan_overlay,
)
from tests.nodes.node_transcript_session_scan_effect.store_doubles import (
    DirectoryTranscriptSource,
    InMemoryTranscriptSource,
)

pytestmark = pytest.mark.unit

SID = "013d2012-829d-46aa-955f-458c3f747ee7"
TOKEN = "prompt-token-7f3a91c2"


def _line(**record: Any) -> str:
    return (
        json.dumps(
            {"sessionId": SID, "timestamp": "2026-11-02T10:00:00Z", **record},
            sort_keys=True,
        )
        + "\n"
    )


def _session(n: int, *, prompt: str = "hello") -> bytes:
    first = _line(type="user", promptSource="terminal", message={"content": prompt})
    return (first + "".join(_line(type="progress") for _ in range(n - 1))).encode()


def _subagent(n: int, agent: str = "a1") -> bytes:
    return "".join(
        _line(
            type="assistant", isSidechain=True, agentId=agent, message={"content": []}
        )
        for _ in range(n)
    ).encode()


def _overlay(
    *roots: dict[str, Any], read_max_bytes: int = 1_000_000
) -> ModelTranscriptScanOverlay:
    return load_scan_overlay(
        {},
        explicit={
            "transcript_roots": list(roots),
            "manifest_hash_algorithm": "sha256",
            "read_max_bytes": read_max_bytes,
        },
    )


def _root(name: str = "mem", handler: str = "memory", **extra: Any) -> dict[str, Any]:
    return {"name": name, "handler": handler, "location": f"{name}-location", **extra}


def _scan(
    overlay: ModelTranscriptScanOverlay, **sources: Any
) -> HandlerTranscriptSessionScan:
    return HandlerTranscriptSessionScan(overlay=overlay, sources=sources)


# AC1 (scan part) ---------------------------------------------------------------------------


async def test_a_subagent_file_is_a_subagent_stream_by_its_path_and_by_its_records() -> (
    None
):
    store = InMemoryTranscriptSource(
        {f"{SID}.jsonl": _session(3), f"{SID}/subagents/agent-a1.jsonl": _subagent(2)}
    )
    scan = _scan(_overlay(_root()), mem=store)
    result = await scan.handle(ModelTranscriptScanRequest())
    kinds = {e.relative_path: e.stream_kind for e in result.entries}
    assert kinds == {
        f"{SID}.jsonl": EnumStreamKind.SESSION,
        f"{SID}/subagents/agent-a1.jsonl": EnumStreamKind.SUBAGENT,
    }
    read = await scan.read_text(
        ModelTranscriptReadRequest(
            root_name="mem", relative_path=f"{SID}/subagents/agent-a1.jsonl"
        )
    )
    facts = HandlerTranscriptSessionFacts().handle(
        ModelSessionFactsRequest(transcript_text=read.transcript_text)
    )
    assert facts.stream_kind is EnumStreamKind.SUBAGENT
    assert facts.agent_id == "a1"
    assert facts.session_id == SID


# AC2 ---------------------------------------------------------------------------------------


async def test_two_top_level_and_five_subagent_files_report_two_and_five() -> None:
    objects = {f"s{i}.jsonl": _session(2) for i in range(2)}
    objects.update(
        {f"s0/subagents/agent-{i}.jsonl": _subagent(1, f"a{i}") for i in range(5)}
    )
    result = await _scan(
        _overlay(_root()), mem=InMemoryTranscriptSource(objects)
    ).handle(ModelTranscriptScanRequest())
    assert (result.top_level_count, result.subagent_count) == (2, 5)
    assert result.manifest_count == 7 == len(result.entries)


async def test_the_manifest_count_equals_find_over_the_same_snapshot(
    tmp_path: Path,
) -> None:
    for i in range(3):
        (tmp_path / f"s{i}.jsonl").write_bytes(_session(2))
    (tmp_path / "s0" / "subagents").mkdir(parents=True)
    for i in range(4):
        (tmp_path / "s0" / "subagents" / f"agent-{i}.jsonl").write_bytes(
            _subagent(1, f"a{i}")
        )
    (tmp_path / "notes.txt").write_text("not a transcript")
    overlay = _overlay(_root("disk", "filesystem"))
    result = await _scan(overlay, disk=DirectoryTranscriptSource(tmp_path)).handle(
        ModelTranscriptScanRequest()
    )

    def find(*extra: str) -> int:
        out = subprocess.run(
            ["find", str(tmp_path), "-name", "*.jsonl", *extra],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return len(out.splitlines())

    assert result.manifest_count == find() == 7
    assert result.subagent_count == find("-path", "*/subagents/*") == 4
    assert result.top_level_count == find("-not", "-path", "*/subagents/*") == 3
    assert result.archive_object_counts == {"disk": 1}


async def test_a_file_in_two_roots_has_one_stream_id_and_the_higher_revision_wins() -> (
    None
):
    older = InMemoryTranscriptSource(
        {"a/s.jsonl": _session(3), "a/only.jsonl": _session(1)}
    )
    newer = InMemoryTranscriptSource({"b/s.jsonl": _session(5)})
    overlay = _overlay(_root("old", prefix="a"), _root("new", prefix="b"))
    result = await _scan(overlay, old=older, new=newer).handle(
        ModelTranscriptScanRequest()
    )
    by_path = {e.relative_path: e for e in result.entries}
    assert set(by_path) == {"s.jsonl", "only.jsonl"}
    shared = by_path["s.jsonl"]
    assert shared.root_name == "new"
    assert shared.present_in == ("old", "new")
    assert shared.source_revision.record_count == 5
    assert shared.stream_id == stream_id_of("s.jsonl", "sha256")
    assert result.listed_transcript_count == 3
    assert result.manifest_count == 2


@pytest.mark.parametrize("prefix", ["transcripts/mac", "transcripts/mac/"])
async def test_a_sibling_prefix_key_is_not_attributed_to_the_root(prefix: str) -> None:
    """The port lists by string prefix, so `transcripts/mac` also lists `transcripts/mac2/...`.

    A key belongs to a root only when it equals the prefix or continues it after a `/`.
    """
    store = InMemoryTranscriptSource(
        {
            "transcripts/mac/s1.jsonl": _session(2),
            "transcripts/mac/archive-20260101.tar": b"x",
            "transcripts/mac2/s9.jsonl": _session(3),
            "transcripts/mac2/archive-20260301.tar": b"y",
        }
    )
    # the store really does list the sibling: the boundary is the root's job, not the port's
    assert "transcripts/mac2/s9.jsonl" in {
        r.key for r in await store.list_objects("transcripts/mac")
    }
    scan = _scan(_overlay(_root("mac", prefix=prefix)), mac=store)
    result = await scan.handle(ModelTranscriptScanRequest())
    assert [e.relative_path for e in result.entries] == ["s1.jsonl"]
    assert result.listed_transcript_count == 1
    assert result.root_transcript_counts == {"mac": 1}
    assert result.archive_object_counts == {"mac": 1}
    assert result.newest_archive_key == {"mac": "transcripts/mac/archive-20260101.tar"}


async def test_a_sibling_prefix_stream_is_attributed_only_to_its_own_root() -> None:
    store = InMemoryTranscriptSource(
        {
            "transcripts/mac/s1.jsonl": _session(2),
            "transcripts/mac2/s9.jsonl": _session(3),
        }
    )
    overlay = _overlay(
        _root("mac", prefix="transcripts/mac"),
        _root("mac2", prefix="transcripts/mac2"),
    )
    scan = _scan(overlay, mac=store, mac2=store)
    result = await scan.handle(ModelTranscriptScanRequest())
    by_path = {e.relative_path: e for e in result.entries}
    assert set(by_path) == {"s1.jsonl", "s9.jsonl"}
    assert by_path["s9.jsonl"].root_name == "mac2"
    assert by_path["s9.jsonl"].present_in == ("mac2",)
    assert by_path["s9.jsonl"].key == "transcripts/mac2/s9.jsonl"
    # every manifest entry reads back from the root and key it names, no KeyError
    for entry in result.entries:
        read = await scan.read_text(
            ModelTranscriptReadRequest(
                root_name=entry.root_name, relative_path=entry.relative_path
            )
        )
        assert read.byte_length > 0


async def test_equal_revisions_keep_the_first_root_and_the_archive_objects_record_their_newest_key() -> (
    None
):
    same = _session(2)
    first = InMemoryTranscriptSource(
        {"s.jsonl": same, "archive-20260101.tar": b"x", "archive-20260301.tar": b"y"}
    )
    second = InMemoryTranscriptSource({"s.jsonl": same})
    result = await _scan(
        _overlay(_root("one"), _root("two")), one=first, two=second
    ).handle(ModelTranscriptScanRequest())
    assert result.entries[0].root_name == "one"
    assert result.newest_archive_key == {"one": "archive-20260301.tar"}
    assert result.archive_object_counts == {"one": 2, "two": 0}
    assert result.manifest_count == 1


async def test_the_scan_is_deterministic_and_unknown_or_unbound_roots_refuse_by_name() -> (
    None
):
    store = InMemoryTranscriptSource({"b.jsonl": _session(2), "a.jsonl": _session(1)})
    scan = _scan(_overlay(_root()), mem=store)
    first = await scan.handle(ModelTranscriptScanRequest())
    assert first == await scan.handle(ModelTranscriptScanRequest())
    assert [e.relative_path for e in first.entries] == ["a.jsonl", "b.jsonl"]
    with pytest.raises(TranscriptRootUnavailableError, match="nope"):
        await scan.handle(ModelTranscriptScanRequest(root_names=("nope",)))
    with pytest.raises(TranscriptRootUnavailableError, match="mem"):
        await _scan(_overlay(_root())).handle(ModelTranscriptScanRequest())


# AC3 ---------------------------------------------------------------------------------------


def test_a_bucket_root_without_a_credential_reference_refuses_at_load_naming_the_key() -> (
    None
):
    with pytest.raises(NodeOverlayError, match=r"transcript_roots\.0\.credential_ref"):
        _overlay(_root("lake", "bucket"))
    assert (
        _overlay(_root("lake", "bucket", credential_ref="transcript-bucket-reader"))
        .transcript_roots[0]
        .credential_ref
        == "transcript-bucket-reader"
    )
    assert _overlay(_root()).transcript_roots[0].credential_ref is None


def test_no_overlay_and_a_root_or_algorithm_that_is_missing_refuse_at_load() -> None:
    # an absent overlay file is refused naming the overlay id and the file, not the key
    with pytest.raises(
        NodeOverlayError, match=r"node_transcript_session_scan_effect/overlay\.yaml"
    ):
        load_scan_overlay({})
    with pytest.raises(NodeOverlayError, match="manifest_hash_algorithm"):
        load_scan_overlay(
            {},
            explicit={
                "transcript_roots": [_root()],
                "manifest_hash_algorithm": "md5",
                "read_max_bytes": 5,
            },
        )
    with pytest.raises(NodeOverlayError, match="transcript_roots"):
        load_scan_overlay(
            {},
            explicit={
                "transcript_roots": [],
                "manifest_hash_algorithm": "sha256",
                "read_max_bytes": 5,
            },
        )


# AC5 ---------------------------------------------------------------------------------------


async def test_a_file_appended_between_scan_and_read_is_stored_under_the_revision_read() -> (
    None
):
    store = InMemoryTranscriptSource({"s.jsonl": _session(10)})
    scan = _scan(_overlay(_root()), mem=store)
    scanned = (await scan.handle(ModelTranscriptScanRequest())).entries[0]
    assert scanned.source_revision.record_count == 10
    store.objects["s.jsonl"] += _line(type="progress").encode()
    read = await scan.read_text(
        ModelTranscriptReadRequest(
            root_name="mem",
            relative_path="s.jsonl",
            scanned_source_revision=scanned.source_revision,
        )
    )
    facts = HandlerTranscriptSessionFacts().handle(
        ModelSessionFactsRequest(
            transcript_text=read.transcript_text,
            required_source_revision=read.required_source_revision,
        )
    )
    assert facts.source_revision.record_count == 11
    assert facts.source_revision.satisfies(
        ModelSourceRevision(record_count=10, byte_length=scanned.size_bytes)
    )


async def test_a_read_shorter_than_the_scan_fails_the_run() -> None:
    store = InMemoryTranscriptSource({"s.jsonl": _session(10)})
    scan = _scan(_overlay(_root()), mem=store)
    scanned = (await scan.handle(ModelTranscriptScanRequest())).entries[0]
    store.objects["s.jsonl"] = _session(9)
    read = await scan.read_text(
        ModelTranscriptReadRequest(
            root_name="mem",
            relative_path="s.jsonl",
            scanned_source_revision=scanned.source_revision,
        )
    )
    with pytest.raises(SourceRevisionShortError):
        HandlerTranscriptSessionFacts().handle(
            ModelSessionFactsRequest(
                transcript_text=read.transcript_text,
                required_source_revision=read.required_source_revision,
            )
        )


async def test_a_read_over_read_max_bytes_refuses_and_a_missing_key_raises_key_error() -> (
    None
):
    store = InMemoryTranscriptSource({"s.jsonl": _session(10)})
    scan = _scan(_overlay(_root(), read_max_bytes=20), mem=store)
    with pytest.raises(TranscriptReadTooLargeError, match="read_max_bytes is 20"):
        await scan.read_text(
            ModelTranscriptReadRequest(root_name="mem", relative_path="s.jsonl")
        )
    with pytest.raises(KeyError):
        await scan.read_text(
            ModelTranscriptReadRequest(root_name="mem", relative_path="gone.jsonl")
        )


# AC6 ---------------------------------------------------------------------------------------


async def test_a_longer_copy_scans_greater_even_when_its_appended_record_has_an_earlier_timestamp() -> (
    None
):
    base = _session(4)
    later_stamp = (
        base + _line(type="progress", timestamp="2020-01-01T00:00:00Z").encode()
    )
    truncated = base.rsplit(b"\n", 2)[0] + b"\n"
    store = InMemoryTranscriptSource(
        {"short.jsonl": truncated, "base.jsonl": base, "long.jsonl": later_stamp}
    )
    result = await _scan(_overlay(_root()), mem=store).handle(
        ModelTranscriptScanRequest()
    )
    rev = {e.relative_path: e.source_revision for e in result.entries}
    assert rev["short.jsonl"] < rev["base.jsonl"] < rev["long.jsonl"]
    assert [
        rev[k].record_count for k in ("short.jsonl", "base.jsonl", "long.jsonl")
    ] == [3, 4, 5]


# AC7 ---------------------------------------------------------------------------------------


async def test_no_prompt_text_appears_in_any_serialized_output() -> None:
    store = InMemoryTranscriptSource(
        {"s.jsonl": _session(3, prompt=f"please do {TOKEN}")}
    )
    scan = _scan(_overlay(_root()), mem=store)
    manifest = await scan.handle(ModelTranscriptScanRequest())
    read = await scan.read_text(
        ModelTranscriptReadRequest(root_name="mem", relative_path="s.jsonl")
    )
    facts = HandlerTranscriptSessionFacts().handle(
        ModelSessionFactsRequest(transcript_text=read.transcript_text)
    )
    assert (
        TOKEN in read.transcript_text
    )  # the positive control: the token is in the bytes read
    for output in (manifest, facts):
        assert TOKEN not in output.model_dump_json()
    assert (
        facts.first_prompt_sha256
        == hashlib.sha256(f"please do {TOKEN}".encode()).hexdigest()
    )


# AC8 ---------------------------------------------------------------------------------------


def _grep_count(files: list[Path], pattern: str) -> tuple[int, int]:
    """The sum of per-file matching lines and the number of files with a match, by grep -c."""
    total = containing = 0
    for path in files:
        proc = subprocess.run(
            ["grep", "-c", pattern, str(path)], capture_output=True, text=True
        )
        assert proc.returncode in (0, 1), proc.stderr
        count = int(proc.stdout.strip())
        total += count
        containing += 1 if count else 0
    return total, containing


async def test_pr_link_and_compaction_totals_equal_the_independent_counts(
    tmp_path: Path,
) -> None:
    layout = {
        "s1.jsonl": [("pr-link", 2), ("compact_boundary", 1)],
        "s2.jsonl": [("pr-link", 1)],
        "s3.jsonl": [("compact_boundary", 3)],
        "s4.jsonl": [],
        "s1/subagents/agent-1.jsonl": [("pr-link", 1)],
    }
    for name, kinds in layout.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = _line(type="user", promptSource="terminal", message={"content": "go"})
        for kind, times in kinds:
            body += _line(type=kind, prUrl="https://example.test/pr/1") * times
        path.write_text(body)
    overlay = _overlay(_root("disk", "filesystem"))
    scan = _scan(overlay, disk=DirectoryTranscriptSource(tmp_path))
    manifest = await scan.handle(ModelTranscriptScanRequest())
    pr_total = compaction_total = pr_files = compaction_files = 0
    for entry in manifest.entries:
        read = await scan.read_text(
            ModelTranscriptReadRequest(
                root_name=entry.root_name, relative_path=entry.relative_path
            )
        )
        facts = HandlerTranscriptSessionFacts().handle(
            ModelSessionFactsRequest(transcript_text=read.transcript_text)
        )
        pr_total += facts.pr_link_count
        compaction_total += facts.compact_boundary_count
        pr_files += 1 if facts.pr_link_count else 0
        compaction_files += 1 if facts.compact_boundary_count else 0
    files = sorted(tmp_path.rglob("*.jsonl"))
    assert (pr_total, pr_files) == _grep_count(files, '"type": "pr-link"') == (4, 3)
    assert (
        (compaction_total, compaction_files)
        == _grep_count(files, '"type": "compact_boundary"')
        == (4, 2)
    )


def test_the_independent_count_helper_sees_a_difference(tmp_path: Path) -> None:
    """The positive control: the helper reports a count of zero for a file with no match."""
    path = tmp_path / "x.jsonl"
    path.write_text('{"type": "user"}\n')
    assert _grep_count([path], '"type": "pr-link"') == (0, 0)
    assert shutil.which("grep") is not None


async def test_a_handler_built_with_no_arguments_refuses_at_first_use_naming_what_is_missing() -> (
    None
):
    """The runtime builds the handler before the overlay and stores are bound; use refuses by name."""
    with pytest.raises(NodeOverlayError, match="node_transcript_session_scan_effect"):
        await HandlerTranscriptSessionScan(environ={}).handle(
            ModelTranscriptScanRequest()
        )
    with pytest.raises(TranscriptRootUnavailableError, match="no transcript store"):
        await HandlerTranscriptSessionScan(overlay=_overlay(_root())).handle(
            ModelTranscriptScanRequest()
        )
