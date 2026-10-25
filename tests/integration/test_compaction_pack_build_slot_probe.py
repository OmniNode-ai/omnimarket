# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21334: one compaction against a live pre-PR runtime slot returns a built package.

The probe runs ``onex run-node node_context_compaction_orchestrator`` inside the
slot's effects runtime, the way AC1's falsifier names it, so the command reaches
the slot's own bus and the handler the runtime registered answers it. It skips
when no pre-PR slot is up on the host, so hosted CI never runs it; the lab pool's
``prepr_runtime_pool.py verify --tests`` runs it on a slot built from the ref.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

_SLOT_MARK = "prepr"
_NODE_ID = "node_context_compaction_orchestrator"
_RUN_NODE_TIMEOUT_S = 90
_LOG_TAIL_LINES = 400


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _slot_effects_container() -> str | None:
    """The running effects runtime of a pre-PR slot, found by its compose project."""
    if shutil.which("docker") is None:
        return None
    listed = _docker(
        "ps", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'
    )
    for row in listed.stdout.splitlines():
        name, _, project = row.partition("\t")
        if "runtime-effects" in name and _SLOT_MARK in project:
            return name
    return None


def _compaction_log_lines(container: str) -> list[str]:
    logs = _docker("logs", "--tail", str(_LOG_TAIL_LINES), container)
    return [
        line[:400]
        for line in (logs.stdout + logs.stderr).splitlines()
        if "compaction" in line.lower() or "pack_builder" in line.lower()
    ][-25:]


def test_compaction_on_a_slot_returns_a_built_package() -> None:
    container = _slot_effects_container()
    if container is None:
        pytest.skip("no pre-PR runtime slot is up on this host")
    command = {
        "invocation_id": str(uuid4()),
        "session_id": f"omn21334-probe-{uuid4()}",
        "cwd": "/probe",
        "trigger": "manual",
        "input_messages_hash": hashlib.sha256(b"omn21334-probe").hexdigest(),
    }
    ran = _docker(
        "exec",
        container,
        "onex",
        "run-node",
        _NODE_ID,
        "--input",
        json.dumps(command),
        "--timeout",
        str(_RUN_NODE_TIMEOUT_S),
        timeout=_RUN_NODE_TIMEOUT_S + 30,
    )
    evidence = {
        "container": container,
        "exit": ran.returncode,
        "stdout": ran.stdout[-1500:],
        "stderr": ran.stderr[-800:],
        "runtime_log": _compaction_log_lines(container),
    }
    print("COMPACTION-PROBE " + json.dumps(evidence, sort_keys=True))
    assert ran.returncode == 0, f"run-node failed: {json.dumps(evidence)}"
    reply = json.loads(ran.stdout.strip().splitlines()[-1])
    body = reply.get("payload", reply)
    assert body.get("status") == "built", f"native fallback: {json.dumps(evidence)}"
    assert body.get("package_id"), f"built reply carries no package id: {body}"
