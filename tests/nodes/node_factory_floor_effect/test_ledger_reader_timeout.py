# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21733: the ledger reader's time limit is the contract's, and a refusal names it."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect import source_loader
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    read_ledger_rows,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(source_loader.__file__).resolve().parent / "contract.yaml"


def test_the_contract_declares_the_reader_time_limit() -> None:
    runtime = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime"]
    assert runtime["ledger_timeout_seconds"] > 300


def test_a_reader_that_runs_past_the_limit_is_refused_naming_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "onex_ledger.py").write_text("", encoding="utf-8")
    seen: dict[str, Any] = {}

    def slow(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(source_loader.subprocess, "run", slow)
    with pytest.raises(SourceRefusedError, match=r"did not finish within 77 s"):
        read_ledger_rows(
            window_days=14,
            cut_chars=1200,
            timeout_seconds=77,
            plugin_root=tmp_path,
            plugin_root_env="CLAUDE_PLUGIN_ROOT",
            ledger_rows_file=None,
        )
    assert seen["timeout"] == 77
