# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: the facts the fill relies on, with a real POSIX leader process (OMN-22848, plan T15).

The leader is the node's entry, run as its own process. The host is this machine (the transport's
local mode, no ssh); the engine is a stub that records its session and process group, started by
the transport under a ``setsid()`` exec shim so the test runs the same on macOS (no ``setsid``
binary) and Linux. The ledger writer is a fixture command that appends to a ledger file and
records which process called it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    PRINCIPAL,
    cells,
    snapshot,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="POSIX sessions only"),
]

SETSID_SHIM = [
    sys.executable,
    "-c",
    "import os,sys;os.setsid();os.execvp(sys.argv[1],sys.argv[1:])",
]
WRITER = """
import os, sys
ledger, rows = sys.argv[1], sys.argv[sys.argv.index("--append") + 1]
with open(ledger, "a", encoding="utf-8") as fh:
    fh.write(rows.rstrip("\\n") + "\\n")
with open(ledger + ".writers", "a", encoding="utf-8") as fh:
    fh.write(f"{os.getppid()} {rows.split(' | ')[1]}\\n")
"""
ENGINE = """
import json, os, time
with open(os.environ["LANE_FACTS"], "w", encoding="utf-8") as fh:
    json.dump({"pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgid(0)}, fh)
time.sleep(1)
print(json.dumps({"type": "result", "result": "LANE_RESULT outcome=done\\nDELEGATION delegated=0 reason=no-text-or-code"}))
"""


def test_fill_contract_facts(tmp_path: Path) -> None:
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")
    engine = tmp_path / "engine.py"
    engine.write_text(ENGINE, encoding="utf-8")
    facts = tmp_path / "engine-facts.json"
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# fixture ledger\n", encoding="utf-8")
    host = socket.gethostname().split(".", 1)[0]
    pool = tmp_path / "pool.json"
    pool.write_text(snapshot(name=host, local=True).model_dump_json(), encoding="utf-8")
    overlay = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    overlay["remote_lane_start"] |= {
        "engine_argv": [sys.executable, str(engine), "{lane}"],
        "engine_environment": {"LANE_FACTS": str(facts)},
        "poll_interval_seconds": 0.2,
        "receipt_dir": str(tmp_path / "receipts"),
        "host_run_root": str(tmp_path / "runs"),
        "detach_argv": SETSID_SHIM,
        "ledger_append_argv": [sys.executable, str(writer)],
        "ledger_writer_principal": PRINCIPAL,
    }
    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("print the host name and stop\n", encoding="utf-8")

    leader = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start",
            "--overlay",
            str(overlay_path),
            "--ledger",
            str(ledger),
            "--pool-snapshot",
            str(pool),
            "--lane",
            "floor-alpha-a1",
            "--key",
            "floor:alpha:alpha-t1",
            "--ticket",
            "OMN-22848",
            "--model",
            "sonnet",
            "--brief",
            str(brief),
            "--run-id",
            "rlane-floor-alpha-a1-fact01",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = leader.communicate(timeout=120)
    assert leader.returncode == 0, out + err

    [claim] = [
        r for r in ledger.read_text(encoding="utf-8").splitlines() if " | CLAIM | " in r
    ]
    assert cells(claim)["lane"] == "floor-alpha-a1"
    assert cells(claim)["key"] == "floor:alpha:alpha-t1"
    writers = (tmp_path / "ledger.md.writers").read_text(encoding="utf-8").split()
    assert writers[0::2] == [str(leader.pid)] * (len(writers) // 2), (
        "only the leader appends"
    )
    assert writers[1::2] == ["CLAIM", "TERMINAL"]

    engine_facts = json.loads(facts.read_text(encoding="utf-8"))
    leader_sid, leader_pgid = (
        os.getsid(0),
        os.getpgid(0),
    )  # the leader inherited both from this test
    assert engine_facts["sid"] != leader_sid, "the engine runs in a session of its own"
    assert engine_facts["pgid"] != leader_pgid, (
        "the engine is not in the leader's process group"
    )
    receipt = json.loads(
        (
            tmp_path / "receipts/floor-alpha-a1/rlane-floor-alpha-a1-fact01.json"
        ).read_text()
    )
    assert receipt["final"]
    assert receipt["engine_rc"] == 0
    assert " lane=floor-alpha-a1 host=" in next(
        line for line in out.splitlines() if line.startswith("CLAIM ")
    )  # the script's own CLAIM line
