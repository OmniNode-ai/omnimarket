# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scope derivation preserves exact named paths and declared code-edit checks."""

from __future__ import annotations

from omnimarket.nodes.node_code_edit_scope_compute.handler import MAX_NAMED_FILES
from omnimarket.nodes.node_code_edit_scope_compute.node import derive_scope

TRACKED = ["README.md", "src/app.py", "src/util.py", "tests/test_app.py", "docs/a b.md"]


def test_named_tracked_files_become_exact_writable_globs() -> None:
    task = "In `src/app.py` fix the parser, then update ./README.md and src/app.py again; see tests/test_app.py."
    writable, _ = derive_scope(task, TRACKED, "/usr/bin/python3")
    assert writable == ["src/app.py", "README.md", "tests/test_app.py"]


def test_checks_are_a_whitespace_check_and_a_compile_of_the_named_python() -> None:
    _, checks = derive_scope(
        "edit src/app.py and README.md", TRACKED, "/opt/my py/python3"
    )
    assert checks == [
        "diff=git diff --check",
        "syntax='/opt/my py/python3' -m py_compile src/app.py",
    ]


def test_a_task_with_no_python_file_gets_only_the_whitespace_check() -> None:
    assert derive_scope("reword README.md", TRACKED, "python3") == (
        ["README.md"],
        ["diff=git diff --check"],
    )


def test_a_task_naming_no_tracked_file_derives_nothing() -> None:
    task = "fix the parser in src/missing.py and /etc/passwd and ../README.md; see https://x.test/src/app.py.bak"
    assert derive_scope(task, TRACKED, "python3") == ([], [])


def test_the_write_scope_is_capped() -> None:
    tracked = [f"f{i}.py" for i in range(MAX_NAMED_FILES + 5)]
    writable, _ = derive_scope(" ".join(tracked), tracked, "python3")
    assert writable == tracked[:MAX_NAMED_FILES]


def test_a_glob_character_in_a_tracked_name_is_never_a_writable_glob() -> None:
    assert derive_scope("edit src/*.py now", ["src/*.py", "src/app.py"], "python3") == (
        [],
        [],
    )


async def test_runtime_command_derives_scope_through_the_declared_topics() -> None:
    """The installed contract routes a wire payload and publishes its typed result."""
    import asyncio
    import json
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    node_name = "node_code_edit_scope_compute"
    contracts = tuple(c for c in discover_contracts().contracts if c.name == node_name)
    assert len(contracts) == 1
    from pathlib import Path

    import yaml

    from omnimarket.nodes.node_code_edit_scope_compute.node import CONTRACT

    raw = yaml.safe_load(Path(CONTRACT).read_text())
    command_topic = raw["runtime_dispatch"]["command_topic"]
    completed_topic = raw["terminal_event"]
    completed: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="code-edit-scope-wiring")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await completed.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed_topic, group_id="scope-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            node_name: "wired"
        }
        assert report.results[0].topics_subscribed == (command_topic,)
        engine.freeze()
        for task, expected in [
            ("edit src/app.py", ["src/app.py"]),
            ("edit unknown.py", []),
        ]:
            correlation_id = uuid4()
            envelope = ModelEventEnvelope[object](
                payload={"task": task, "tracked_files": TRACKED, "python": "python3"},
                correlation_id=correlation_id,
                event_type="omnimarket.code-edit-scope-requested",
            )
            await bus.publish(
                command_topic, None, envelope.model_dump_json().encode(), None
            )
            event = await asyncio.wait_for(completed.get(), timeout=3)
            assert event["correlation_id"] == str(correlation_id)
            result = event["payload"]
            assert result["writable"] == expected
            assert result["checks"] == (
                ["diff=git diff --check", "syntax=python3 -m py_compile src/app.py"]
                if expected
                else []
            )
    finally:
        await bus.close()
