# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23867): the effect executes only a typed command it is sent; an undeclared host or class is refused typed."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_storage_effect.handlers.backend_in_memory import (
    BackendHostStorageInMemory,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
    ModelHostStorageRequest,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
)

from .disk_inputs import (
    CAPACITY,
    HOST,
    NODE_DIR,
    make_dirs,
    make_entry,
    overlay_document,
    real_handler,
    request,
)

pytestmark = pytest.mark.unit


def test_a_prune_of_one_docker_class_runs_its_declared_command_and_reads_the_bytes(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path)
    result = real_handler(dirs, no_holders=True).handle(
        request("prune_docker_build_cache")
    )
    assert result.outcome is EnumHostStorageOutcome.OK
    assert result.entry_results == ()
    assert result.freed_bytes == 3000
    assert not (dirs.docker_stand_in / "layer.bin").exists()


@pytest.mark.parametrize(
    ("host", "command", "refusal"),
    [
        ("pool-unknown", "evict_cache", EnumHostStorageRefusal.HOST_UNKNOWN),
        (HOST, "prune_docker_volumes", EnumHostStorageRefusal.COMMAND_UNKNOWN),
        (
            "pool-b",
            "evict_cache",
            EnumHostStorageRefusal.COMMAND_NOT_DECLARED_FOR_HOST,
        ),
    ],
)
def test_a_host_or_class_the_overlay_does_not_declare_is_refused_and_nothing_is_removed(
    tmp_path: Path, host: str, command: str, refusal: EnumHostStorageRefusal
) -> None:
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    result = real_handler(dirs, no_holders=True).handle(
        request(command, entry, host=host)
    )
    assert result.outcome is EnumHostStorageOutcome.REFUSED
    assert result.refusal is refusal
    assert result.entry_results == ()
    assert entry.exists()


def test_an_entry_command_with_no_entries_and_an_argv_command_with_entries_are_refused(
    tmp_path: Path,
) -> None:
    dirs = make_dirs(tmp_path)
    handler = real_handler(dirs, no_holders=True)
    assert (
        handler.handle(request("evict_cache")).refusal
        is EnumHostStorageRefusal.ENTRIES_REQUIRED
    )
    entry = make_entry(dirs.cache, "pkg-a")
    result = handler.handle(request("prune_docker_build_cache", entry))
    assert result.refusal is EnumHostStorageRefusal.ENTRIES_NOT_ACCEPTED
    assert entry.exists()
    assert (dirs.docker_stand_in / "layer.bin").exists()


def test_the_handler_does_nothing_until_it_is_sent_a_command(tmp_path: Path) -> None:
    dirs = make_dirs(tmp_path)
    entry = make_entry(dirs.cache, "pkg-a")
    overlay = ModelHostStorageOverlay.model_validate(
        overlay_document(dirs, backend="in_memory")
    )
    backend = BackendHostStorageInMemory(overlay)
    handler = HandlerHostStorage(overlay, backend)
    assert backend.ran == []
    assert entry.exists()
    # one command in, one run out: the effect acts on the command and on nothing else
    handler.handle(
        ModelHostStorageRequest(
            host=HOST, command="prune_docker_build_cache", requested_by="lane-a"
        )
    )
    assert backend.ran == [(HOST, "prune_docker_build_cache")]
    assert entry.exists()


def test_no_source_of_the_node_schedules_anything_or_acts_on_its_own_clock() -> None:
    forbidden_modules = {"sched", "schedule", "apscheduler"}
    offenders: list[str] = []
    for path in sorted(NODE_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}: import {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in forbidden_modules
                ]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden_modules:
                    offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Attribute) and (
                (node.attr == "sleep" and isinstance(node.value, ast.Name))
                or node.attr in {"Timer", "call_later", "call_at"}
            ):
                offenders.append(f"{path.name}:{node.lineno}: {node.attr}")
    assert offenders == []


def test_the_in_memory_overlay_declares_the_entry_commands_and_the_suite_reads_it() -> (
    None
):
    import yaml

    overlay = ModelHostStorageOverlay.model_validate(
        yaml.safe_load(
            (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
        )
    )
    host = overlay.host("sim-host-a")
    assert host is not None
    handler = HandlerHostStorage(overlay, BackendHostStorageInMemory(overlay))
    result = handler.handle(
        ModelHostStorageRequest(
            host="sim-host-a",
            command="evict_cache",
            requested_by="lane-a",
            entries=("/sim/cache/pkg-old", "/sim/cache/pkg-busy", "/sim/elsewhere/x"),
        )
    )
    assert result.outcome is EnumHostStorageOutcome.PARTIAL
    assert (result.free_bytes_before, result.free_bytes_after) == (20000, 27000)
    assert CAPACITY > 0
