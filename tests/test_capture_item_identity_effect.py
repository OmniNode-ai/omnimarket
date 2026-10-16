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
import sys
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


def _issue(args: tuple[str, str, int]) -> list[int]:
    directory, stream, count = args
    return [
        ident.next_producer_seq(Path(directory), SESSION, stream) for _ in range(count)
    ]


def test_eight_concurrent_writers_issue_1000_gap_free_sequence_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A loaded runner must not turn this into a lock-timeout test.
    monkeypatch.setenv("ONEX_HOOK_LOCK_WAIT_S", "60")
    jobs = [(str(tmp_path), ident.STREAM_CONTENT, 125)] * 8
    with multiprocessing.get_context("fork").Pool(8) as pool:
        issued = [n for batch in pool.map(_issue, jobs) for n in batch]
    assert sorted(issued) == list(range(1, 1001))
    assert ident.high_water(tmp_path, SESSION, ident.STREAM_CONTENT) == 1000
    assert ident.high_water(tmp_path, SESSION, ident.STREAM_LINEAGE) == 0


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
