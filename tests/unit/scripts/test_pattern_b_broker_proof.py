"""Tests for the Pattern B broker proof runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_pattern_b_broker_proof_runner_inmemory() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/proof_pattern_b_broker.py",
            "--mode",
            "inmemory",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "inmemory"
    assert payload["status"] == "passed"
    assert payload["dispatch_events"] == 1
    assert payload["terminal_events"] == 1
    assert payload["terminal_event"]["status"] == "completed"


def test_pattern_b_broker_proof_runner_host_live_skips_without_bootstrap() -> None:
    env = os.environ.copy()
    env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/proof_pattern_b_broker.py",
            "--mode",
            "host-live",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "host-live"
    assert payload["status"] == "skipped"
    assert "KAFKA_BOOTSTRAP_SERVERS" in payload["reason"]
