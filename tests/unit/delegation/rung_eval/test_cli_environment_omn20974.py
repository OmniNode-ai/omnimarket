# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CLI supplies the pinned transport's environment and persists refusals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.delegation.rung_eval.rungs import load_rungs

pytestmark = pytest.mark.unit


def test_cli_without_rung_bindings_writes_unresolved_rows(tmp_path: Path) -> None:
    rungs = load_rungs()
    env = dict(os.environ)
    for rung in rungs:
        for name in (
            rung.base_url_env,
            rung.model_env,
            rung.api_key_env,
            rung.backend_id_env,
            rung.tenant_env,
        ):
            if name is not None:
                env.pop(name, None)
    root = Path(__file__).resolve().parents[4]
    done = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/ci/run_delegation_rung_eval.py"),
            "run",
            "--night",
            "2026-10-15",
            "--out-dir",
            str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    rows = [json.loads(line) for line in done.stdout.splitlines()]
    assert {row["rung_id"] for row in rows} == {rung.rung_id for rung in rungs}
    assert all(row["status"] == "unresolved" for row in rows)
    cloud = next(rung for rung in rungs if rung.backend_id_env is not None)
    assert all(
        cloud.backend_id_env in row["detail"]
        for row in rows
        if row["rung_id"] == cloud.rung_id
    )
    stored = tmp_path / "rung_eval_2026-10-15.jsonl"
    assert [json.loads(line) for line in stored.read_text().splitlines()] == rows
