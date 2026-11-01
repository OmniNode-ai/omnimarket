# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A location comes from an injected setting, never from a literal in the code (OMN-21694)."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "benchmarks" / "delegation_false_pass_growth"
WORK_DIR_SETTING = "OMN20032_WORK_DIR"

# The pattern of the contracts_and_di literal_path rule.
_PATH_ROOTS = (
    "Users",
    "Volumes",
    "home",
    "opt",
    "srv",
    "var",
    "etc",
    "private",
    "tmp",
    "mnt",
)
LITERAL_PATH = re.compile(
    "^(?:" + "|".join(f"/{root}/" for root in _PATH_ROOTS) + r"|~/)\S*$"
)

BENCH_SCRIPTS = [
    "audit_generated_rows.py",
    "candidates_meta.py",
    "gen_runner_inmemory.py",
    "gen_runner_persisted_path.py",
    "generation_summary.py",
    "make_local_only_contract.py",
    "make_local_only_routing.py",
    "make_local_overlays.py",
    "mem_to_content.py",
]

SLICE_SOURCES = [
    f"benchmarks/delegation_false_pass_growth/{name}" for name in BENCH_SCRIPTS
]


def _literal_paths(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and LITERAL_PATH.match(node.value)
    ]


@pytest.mark.parametrize("relative", SLICE_SOURCES)
def test_the_touched_module_holds_no_path_literal(relative: str) -> None:
    assert _literal_paths(REPO / relative) == []


def test_the_scan_sees_a_path_literal(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text('where = "/opt/example/dir"\nname = "plain"\n', encoding="utf-8")
    assert _literal_paths(probe) == ["/opt/example/dir"]


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != WORK_DIR_SETTING}
    env.pop("ONEX_TENANT_ID", None)
    env.update(extra)
    return env


# These two read a file that exists only inside the lab container before they reach the setting.
CONTAINER_FIRST = {"make_local_only_contract.py", "make_local_only_routing.py"}


@pytest.mark.parametrize("script", BENCH_SCRIPTS)
def test_a_benchmark_script_reads_the_work_dir_as_a_required_setting(
    script: str,
) -> None:
    tree = ast.parse((BENCH / script).read_text(encoding="utf-8"))
    required = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == WORK_DIR_SETTING
    ]
    assert required, f"{script} does not require {WORK_DIR_SETTING}"


@pytest.mark.parametrize(
    "script", [name for name in BENCH_SCRIPTS if name not in CONTAINER_FIRST]
)
def test_a_benchmark_script_refuses_without_a_work_dir(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(BENCH / script), "x", "1", "1", "none"],
        env=_clean_env(
            ONEX_TENANT_ID="tenant-under-test",
            BACKEND_A_CHAT_URL="http://backend-a.invalid/v1",
            BACKEND_B_CHAT_URL="http://backend-b.invalid/v1",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert WORK_DIR_SETTING in result.stderr


def test_the_overlay_script_writes_under_the_injected_work_dir(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(BENCH / "make_local_overlays.py")],
        env=_clean_env(
            **{
                WORK_DIR_SETTING: str(tmp_path),
                "BACKEND_A_CHAT_URL": "http://backend-a.invalid/v1",
                "BACKEND_B_CHAT_URL": "http://backend-b.invalid/v1",
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == [
        "dev.local.bifrost.yaml",
        "dev.local202.bifrost.yaml",
        "dev.local202b.bifrost.yaml",
    ]
