# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canary admission and whole-set installation preserve the original operation results."""

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_install_canary_effect import NodeInstallCanaryEffect
from omnimarket.nodes.node_install_canary_effect.handlers.handler_install_canary import (
    HandlerInstallCanary,
    ModelInstallCanaryRequest,
)

pytestmark = pytest.mark.unit


def reading(name, *, local=False, parity=None, error=None, busy=None):
    return SimpleNamespace(
        host=SimpleNamespace(name=name, local=local),
        error=error,
        parity_refusal=parity,
        admission_refusal=busy or parity,
    )


def test_incident_a_pool_read_admitting_no_lab_host_fails_the_canary():
    incident = "PARITY-FAILED: clone repo-private-data is missing"
    hosts = ("host-a", "host-b", "host-c", "host-d")
    readings = [
        reading("local", local=True),
        *(reading(h, parity=incident) for h in hosts),
    ]
    ok, lines = HandlerInstallCanary({}).pool_verdict(readings, min_admitted=1)
    assert ok is False
    assert lines[-1] == "CANARY-POOL admitted=0 lab=4 min=1"
    assert lines[:4] == [f"CANARY-POOL {h} refused {incident}" for h in hosts]


def test_pool_read_admits_a_busy_host_and_not_an_unreadable_one():
    readings = [
        reading("host-a", busy="LOAD"),
        reading("host-b", error="ssh timeout"),
        reading("host-c", parity="missing clone"),
    ]
    handler = HandlerInstallCanary({})
    ok, lines = handler.pool_verdict(readings, min_admitted=1)
    assert ok is True
    assert lines[-1] == "CANARY-POOL admitted=1 lab=3 min=1"
    assert "CANARY-POOL host-b unreadable ssh timeout" in lines
    assert handler.pool_verdict(readings[1:], min_admitted=1)[0] is False


def test_typed_boundary_dispatches_the_same_operation():
    result = NodeInstallCanaryEffect({}).handle(
        ModelInstallCanaryRequest("pool_verdict", ([],))
    )
    assert result.value == (False, ["CANARY-POOL admitted=0 lab=0 min=1"])


def test_unknown_operation_never_dispatches_private_methods():
    with pytest.raises(ValueError, match="unknown install operation"):
        NodeInstallCanaryEffect({}).handle(ModelInstallCanaryRequest("_placement"))


def test_placement_is_injected_by_the_caller():
    placement = object()
    assert HandlerInstallCanary({"placement": placement})._placement() is placement


def test_successful_install_replaces_the_whole_set(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    HandlerInstallCanary({}).install_set(
        [(first, b"new-first", "100644"), (second, b"new-second", "100755")]
    )
    assert first.read_bytes() == b"new-first"
    assert second.read_bytes() == b"new-second"
    assert second.stat().st_mode & 0o111


def test_a_failed_stage_keeps_every_previous_file(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    handler = HandlerInstallCanary({})
    stage = handler.stage_file

    def fail_second(dest, data, mode):
        if dest == second:
            raise OSError("fixture refusal")
        return stage(dest, data, mode)

    monkeypatch.setattr(handler, "stage_file", fail_second)
    with pytest.raises(OSError, match="fixture refusal"):
        handler.install_set(
            [(first, b"new-first", "100644"), (second, b"new-second", "100644")]
        )
    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"


def test_runtime_inputs_can_travel_with_an_inmemory_request(tmp_path):
    (tmp_path / "payload").write_bytes(b"content")
    result = NodeInstallCanaryEffect().handle(
        ModelInstallCanaryRequest(
            "tree_sha256", (tmp_path,), runtime={"ignored": lambda _name: False}
        )
    )
    assert len(result.value) == 64
    assert result.value == HandlerInstallCanary(
        {"ignored": lambda _name: False}
    ).tree_sha256(tmp_path)


@pytest.mark.parametrize("failed", [False, True])
def test_install_canary_records_success_or_rolls_back_and_queues_repair(
    tmp_path, failed
):
    record = tmp_path / "canary.json"
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(
        json.dumps(
            {"plugins": {"fixture-plugin": [{"installPath": str(tmp_path / "new")}]}}
        )
    )
    prior_bytes = b'{"plugins": {"fixture-plugin": [{"installPath": "previous"}]}}'
    queued = []
    resets = []
    writer = HandlerInstallCanary({}).write_atomic

    def load(path):
        return json.loads(path.read_text()) if path.exists() else {}

    def save(path, state):
        path.write_text(json.dumps(state))

    placement = SimpleNamespace(
        placement_dir=lambda _env: tmp_path / "queue",
        lane_queue_enqueue=lambda *args, **kw: queued.append((args, kw)),
    )
    handler = HandlerInstallCanary(
        {
            "expand": lambda text, _env: (
                record if text.endswith("canary.json") else None
            ),
            "load_state": load,
            "save_state": save,
            "write_atomic": writer,
            "utc_now": lambda: datetime(2026, 10, 26, tzinfo=UTC),
            "stamp": lambda now: now.isoformat(),
            "emit_event": lambda _path, event: json.dumps(event),
            "placement": placement,
        }
    )
    target = SimpleNamespace(
        name="fixture-target",
        env={},
        spec={
            "plugin": "fixture-plugin",
            "canary": {
                "record": str(record),
                "steps": [
                    {
                        "name": "pool-admission",
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('pool checked'); raise SystemExit("
                            + str(3 if failed else 0)
                            + ")",
                        ],
                    }
                ],
            },
        },
    )
    result = SimpleNamespace(actions=[], errors=[])
    git = SimpleNamespace(run=lambda *args: resets.append(args))
    handler.canary_install(
        target, result, git, installed, prior_bytes, ("b" * 40, "old"), "a" * 40, "new"
    )
    state = load(record)
    assert state["failed" if failed else "passed"]["sha"] == "a" * 40
    assert state["failed" if failed else "passed"]["steps"][0]["rc"] == (
        3 if failed else 0
    )
    if failed:
        assert installed.read_bytes() == prior_bytes
        assert resets == [("reset", "-q", "--keep", "b" * 40)]
        assert len(queued) == 1
        assert queued[0][1]["reason"].startswith("install canary failed")
        assert list(tmp_path.glob("canary-fix-*.md"))
    else:
        assert not resets
        assert not queued
        assert "failed" not in state
