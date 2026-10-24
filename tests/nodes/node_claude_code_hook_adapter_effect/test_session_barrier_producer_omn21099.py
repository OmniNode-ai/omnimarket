# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adapter journals a session barrier before compaction (OMN-21099, TA.21)."""

from __future__ import annotations

import importlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from pydantic import JsonValue

import omnimarket.nodes
from omnimarket.models.claude_code_binding import ModelClientBinding
from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    ModelContextCompactionRequested,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrier,
    ModelSessionStreamBarrierProbe,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    SESSION_BARRIER_SOURCE,
    HandlerClaudeCodeHookAdapter,
    build_command_payload,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    CONTRACT_PATH,
    load_client_section,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_session_barrier_journal import (
    HandlerSessionBarrierJournal,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelHookAdapterRequest,
    ModelReceiptEvent,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_session_barrier_wait import (
    HandlerSessionBarrierWait,
)

pytestmark = pytest.mark.unit

SESSION = "session-1"
INVOCATION = UUID("11111111-1111-4111-8111-111111111111")
BARRIER = UUID("22222222-2222-4222-8222-222222222222")
HOOK_INPUT: dict[str, JsonValue] = {
    "session_id": SESSION,
    "cwd": "/work/repo",
    "trigger": "manual",
    "messages": [{"role": "user", "text": "Summarise the work."}],
}


def _binding() -> ModelClientBinding:
    return next(
        binding
        for binding in load_client_section(CONTRACT_PATH).bindings
        if binding.name == "compaction"
    )


def _seed_counters(journal_dir: Path) -> None:
    """Seed the real item state using only the capture node's declared entrypoints."""
    contract_path = (
        Path(next(iter(omnimarket.nodes.__path__)))
        / "node_capture_item_identity_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    module, _, name = contract["input_model"].rpartition(".")
    request_model = getattr(importlib.import_module(module), name)
    for stream, high_water in (("lineage", 3), ("content", 2)):
        for sequence in range(1, high_water + 1):
            result = handler.handle(
                request_model(
                    operation="next_producer_seq",
                    item_dir=str(journal_dir.parent / "hook_items"),
                    session_id=SESSION,
                    stream=stream,
                )
            )
            assert result.sequence == sequence


def _events(journal_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(journal_dir.glob("*.json"))]


def test_enabled_capture_journals_both_stream_high_waters(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    _seed_counters(journal_dir)
    # A stalled clock must still produce file names in write order.
    journal = HandlerSessionBarrierJournal(
        environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(journal_dir)},
        barrier_ids=lambda: BARRIER,
        time_ns=lambda: 100,
    )
    barrier_id = journal.journal(SESSION)
    files = sorted(journal_dir.iterdir())
    assert len(files) == 2
    assert all(re.fullmatch(r"\d{20}_[0-9a-f-]{36}\.json", p.name) for p in files)
    assert int(files[1].name.split("_")[0]) > int(files[0].name.split("_")[0])
    assert barrier_id == str(BARRIER)
    events = _events(journal_dir)
    assert [e["payload"]["stream"] for e in events] == ["lineage", "content"]
    assert [e["payload"]["high_water"] for e in events] == [3, 2]
    for path, event in zip(files, events, strict=True):
        assert set(event) == {
            "event_id",
            "event_type",
            "payload",
            "correlation_id",
            "queued_at",
        }
        assert event["event_type"] == "content.captured"
        assert str(UUID(event["event_id"])) == event["event_id"]
        assert event["event_id"] in path.name
        assert event["correlation_id"] == SESSION
        assert (
            datetime.fromisoformat(event["queued_at"]).utcoffset().total_seconds() == 0
        )
        payload = event["payload"]
        assert payload["session_id"] == SESSION
        assert payload["content_kind"] == payload["record_type"] == "barrier"
        assert payload["barrier_id"] == barrier_id
    assert events[0]["payload"]["content_capture"] == "enabled"
    assert "content_capture" not in events[1]["payload"]
    assert not list(journal_dir.glob("*.tmp"))


@pytest.mark.parametrize("disabled", ["0", "off", "false", "no", " FALSE "])
def test_disabled_capture_journals_only_lineage(tmp_path: Path, disabled: str) -> None:
    journal_dir = tmp_path / "journal"
    barrier_id = HandlerSessionBarrierJournal(
        environ={
            "ONEX_HOOK_EMIT_JOURNAL_DIR": str(journal_dir),
            "OMNICLAUDE_CONTENT_CAPTURE": disabled,
        }
    ).journal(SESSION)
    (event,) = _events(journal_dir)
    assert event["payload"]["barrier_id"] == barrier_id
    assert event["payload"]["stream"] == "lineage"
    assert event["payload"]["content_capture"] == "disabled"


class _RecordingClient:
    def __init__(self, journal_dir: Path) -> None:
        self.journal_dir = journal_dir
        self.payloads: list[dict[str, JsonValue]] = []
        self.events_at_publish: list[dict[str, Any]] = []

    def publish_and_await(
        self,
        *,
        topic: str,
        command_type: str,
        payload: dict[str, JsonValue],
        awaited_events: tuple[str, ...],
        deadline_ms: int,
    ) -> tuple[ModelReceiptEvent, ...]:
        self.events_at_publish = _events(self.journal_dir)
        self.payloads.append(payload)
        return ()


def test_adapter_journals_before_publishing_the_shared_barrier_id(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    client = _RecordingClient(journal_dir)
    binding = _binding()
    assert (
        next(a.source for a in binding.arguments if a.name == "barrier_id")
        == SESSION_BARRIER_SOURCE
    )
    HandlerClaudeCodeHookAdapter(
        client,
        barrier_journal=HandlerSessionBarrierJournal(
            environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(journal_dir)}
        ),
    ).handle(ModelHookAdapterRequest(binding=binding, hook_input=HOOK_INPUT))
    (command,) = client.payloads
    assert len(client.events_at_publish) == 2
    assert {e["payload"]["barrier_id"] for e in client.events_at_publish} == {
        command["barrier_id"]
    }


class _ProjectedJournal:
    """Read the barriers and pretend all preceding capture records were projected."""

    def __init__(self, journal_dir: Path) -> None:
        self.journal_dir = journal_dir
        self.reads = 0

    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        self.reads += 1
        barriers = tuple(
            ModelSessionStreamBarrier.model_validate(
                {
                    key: payload[key]
                    for key in ("stream", "barrier_id", "high_water", "content_capture")
                    if key in payload
                }
            )
            for event in _events(self.journal_dir)
            if (payload := event["payload"])["content_kind"] == "barrier"
            and payload["session_id"] == session_id
            and payload["barrier_id"] == barrier_id
        )
        return ModelSessionStreamBarrierProbe(
            session_id=session_id,
            barrier_id=barrier_id,
            barriers=barriers,
            source_revision="2026-10-23T12:00:00Z",
            applied={b.stream: tuple(range(1, b.high_water + 1)) for b in barriers},
        )


class _Build:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, command):
        from tests.nodes.node_context_compaction_orchestrator.test_compaction_pack_build import (
            _ok_built,
        )

        self.calls += 1
        return _ok_built()


@pytest.mark.parametrize(
    "with_producer", [False, True], ids=["before-fix", "with-producer"]
)
def test_real_orchestrator_requires_the_produced_barrier(
    tmp_path: Path, with_producer: bool, compaction_bus
) -> None:
    journal_dir = tmp_path / "journal"
    _seed_counters(journal_dir)
    binding = _binding()
    if not with_producer:
        binding = binding.model_copy(
            update={
                "arguments": tuple(
                    argument.model_copy(update={"source": "$.barrier_id"})
                    if argument.name == "barrier_id"
                    else argument
                    for argument in binding.arguments
                )
            }
        )
    command = ModelContextCompactionRequested.model_validate(
        build_command_payload(
            binding,
            HOOK_INPUT,
            invocation_id=INVOCATION,
            barrier_journal=HandlerSessionBarrierJournal(
                environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(journal_dir)}
            ),
        )
    )
    source = _ProjectedJournal(journal_dir)
    build = _Build()
    orchestrator = compaction_bus(build).handler(
        barrier_wait=HandlerSessionBarrierWait(source, deadline_s=0, poll_interval_s=0),
        barrier_probe_source=source,
    )
    result = orchestrator.handle(command)
    if with_producer:
        assert command.barrier_id is not None
        assert result.reply.action == EnumClientVerdictAction.INSTALL
        assert result.fallback is None
        assert build.calls == 1
        assert source.reads == 2
        assert [e["payload"]["high_water"] for e in _events(journal_dir)] == [3, 2]
    else:
        assert command.barrier_id is None
        assert result.reply.action == EnumClientVerdictAction.NATIVE
        assert result.fallback is not None
        assert (
            result.fallback.failure_class
            == EnumContextCompactionFailureClass.SOURCE_INCOMPLETE
        )
        assert build.calls == source.reads == 0
        assert _events(journal_dir) == []


@pytest.mark.parametrize(
    "hook_input",
    [{}, {"session_id": None}, {"session_id": ""}, {"session_id": 42}],
)
def test_reserved_source_rejects_missing_or_invalid_session_id(
    hook_input: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValueError, match="non-empty session_id"):
        build_command_payload(_binding(), hook_input)


def test_reserved_source_requires_an_explicit_journal() -> None:
    with pytest.raises(ValueError, match="barrier_journal"):
        build_command_payload(_binding(), HOOK_INPUT)


def test_journal_rejects_an_empty_session_id(tmp_path: Path) -> None:
    journal = HandlerSessionBarrierJournal(
        environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(tmp_path / "journal")}
    )
    with pytest.raises(ValueError, match="session_id"):
        journal.journal("")


def test_journal_write_failure_prevents_publishing(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    journal_dir.write_text("not a directory")
    client = _RecordingClient(journal_dir)
    adapter = HandlerClaudeCodeHookAdapter(
        client,
        barrier_journal=HandlerSessionBarrierJournal(
            environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(journal_dir)}
        ),
    )
    with pytest.raises(FileExistsError):
        adapter.handle(
            ModelHookAdapterRequest(binding=_binding(), hook_input=HOOK_INPUT)
        )
    assert client.payloads == []


@pytest.mark.parametrize("location", ["explicit", "state", "home"])
def test_journal_directory_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    environ = {"ONEX_HOOK_EMIT_JOURNAL_DIR": "", "ONEX_STATE_DIR": ""}
    if location == "explicit":
        environ.update(
            {
                "ONEX_HOOK_EMIT_JOURNAL_DIR": str(tmp_path / "explicit"),
                "ONEX_STATE_DIR": str(tmp_path / "state"),
            }
        )
        expected = tmp_path / "explicit"
    elif location == "state":
        environ["ONEX_STATE_DIR"] = str(tmp_path / "state")
        expected = tmp_path / "state/hook_emit_journal"
    else:
        expected = tmp_path / "home/.onex_state/hook_emit_journal"
    HandlerSessionBarrierJournal(environ=environ).journal(SESSION)
    assert len(_events(expected)) == 2
