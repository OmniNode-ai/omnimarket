# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-25856: a command check that times out must not leave its process tree running.

The dev runtime runs ``dod_evidence`` command checks inside its own container, under
the container's CPU quota. ``subprocess.run(["bash", "-c", cmd], timeout=...)`` kills only
``bash`` when the timeout trips; ``uv`` and ``pytest`` below it were reparented to the
container init and kept burning CPU for minutes. Three of them held about 2.4 of the
3 CPUs the runtime container may use, the kernel throttled every thread of the runtime
(its event loop and its ``/health`` answer included), and health answers stalled for
0.3 to 12 s. Each check now runs in its own process group, and the whole group is
killed when the timeout trips.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; read the state instead.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


@pytest.mark.unit
def test_timed_out_check_kills_the_processes_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    monkeypatch.setenv("DOD_VERIFY_CHECK_TIMEOUT_S", "1")
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-25856",
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "a check that outlives its timeout",
                "checks": [
                    {
                        "check_type": "command",
                        # ``sleep`` stands in for ``uv run pytest``: a child of the
                        # shell that keeps running after the shell is killed.
                        "check_value": f"sleep 300 & echo $! > {pid_file}; wait",
                    }
                ],
            }
        ],
    }
    contract_path = tmp_path / "OMN-25856.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")

    results = EvidenceCollector().collect("OMN-25856", contract_path=str(contract_path))

    grandchild = int(pid_file.read_text().strip())
    try:
        deadline = time.monotonic() + 3.0
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        survived = _alive(grandchild)
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)

    assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
    assert not survived, (
        f"pid {grandchild} started by the timed-out check is still running"
    )
