# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI-layer RED/GREEN proof for node_contract_sweep (OMN-14542, class fix,
parent OMN-14531).

Mirrors the handler-level scope tests but drives the real
``python -m omnimarket.nodes.node_contract_sweep`` entrypoint end-to-end via
subprocess, exactly as CI/pre-commit will invoke it — proving the roll-up
(``__main__.py``), not just the in-process handler.

- RED: a syntactically valid ``--repos`` value that resolves to zero
  discoverable ``contract.yaml`` files (typo'd repo name) must exit non-zero
  with an explicit refusal message, never a silent, empty-but-successful
  exit 0.
- GREEN: a genuinely-healthy populated scope (a synthetic repo with N valid
  contracts) must exit 0 with ``scanned_count == N`` and zero violations.
- Bare invocation without ``--repos`` must fail argparse (required arg).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_VALID_CONTRACT = textwrap.dedent("""\
    name: node_test_valid
    node_type: COMPUTE_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "A valid test node"
    event_bus:
      publish_topics:
        - "onex.evt.platform.test-event.v1"
""")


def _write_contract(base: Path, node_name: str, content: str) -> Path:
    node_dir = base / "nodes" / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    contract = node_dir / "contract.yaml"
    contract.write_text(content)
    return contract


@pytest.mark.integration
def test_cli_missing_repos_flag_fails_argparse(tmp_path: Path) -> None:
    """--repos is a required CLI flag — bare invocation must fail before
    ever reaching the handler."""
    proc = subprocess.run(
        [sys.executable, "-m", "omnimarket.nodes.node_contract_sweep"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "--repos" in proc.stderr


@pytest.mark.integration
def test_cli_typo_repo_name_is_red(tmp_path: Path) -> None:
    """MANDATORY RED PROOF (CLI layer): a real EXISTS-but-WRONG scope —
    OMNI_HOME resolves, but the requested repo does not exist under it —
    must exit non-zero and print a refusal, never a clean exit 0."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_contract_sweep",
            "--repos",
            "does_not_exist_repo",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "OMNI_HOME": str(tmp_path)},
    )
    assert proc.returncode != 0, (
        f"expected non-zero exit over an unresolvable scope, got 0. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ERROR"
    assert payload["scanned_count"] == 0
    assert "does_not_exist_repo" in payload["scope_error"]
    assert "refusing" in proc.stderr.lower()


@pytest.mark.integration
def test_cli_healthy_populated_scope_is_green(tmp_path: Path) -> None:
    """GREEN PROOF (CLI layer): a genuinely-healthy populated scope reports
    a clean exit 0 with scanned_count matching the planted corpus size."""
    omni_home = tmp_path / "omni_home"
    repo = omni_home / "myrepo" / "src"
    for i in range(3):
        _write_contract(repo, f"node_{i}", _VALID_CONTRACT)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_contract_sweep",
            "--repos",
            "myrepo",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "OMNI_HOME": str(omni_home)},
    )
    assert proc.returncode == 0, (
        f"expected clean exit over a healthy populated scope. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "PASS"
    assert payload["scanned_count"] == 3
    assert payload["violations"] == []
