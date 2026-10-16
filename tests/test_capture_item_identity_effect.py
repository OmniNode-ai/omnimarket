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
