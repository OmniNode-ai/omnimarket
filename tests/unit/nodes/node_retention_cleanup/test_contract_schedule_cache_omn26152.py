# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The retention gate parses its contract schedule once per file content (OMN-26152).

The shared dev runtime's py-spy sample put about 10.6 percent of CPU samples in
``contract_schedule``: the runtime tick calls it on every tick, and each call ran
``yaml.safe_load`` over the whole retention contract. An unchanged file is now parsed
once; a changed file is parsed again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_retention_cleanup as mod,
)

_TEMPLATE = """\
retention_policy:
  schedule:
    run_interval_seconds: {interval}
    database_url_env: ONEX_TEST_DB
"""


@pytest.fixture
def parse_count(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = yaml.safe_load

    def counting(stream: str) -> object:
        calls[0] += 1
        return real(stream)

    monkeypatch.setattr(mod.yaml, "safe_load", counting)
    return calls


def test_unchanged_contract_is_parsed_once_across_ticks(
    tmp_path: Path, parse_count: list[int]
) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text(_TEMPLATE.format(interval=3600))
    first = mod.contract_schedule(path)
    for _ in range(200):
        assert mod.contract_schedule(path) == first
    assert parse_count[0] == 1


def test_changed_contract_is_parsed_again(
    tmp_path: Path, parse_count: list[int]
) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text(_TEMPLATE.format(interval=3600))
    assert mod.contract_schedule(path).run_interval_seconds == 3600
    path.write_text(_TEMPLATE.format(interval=7200))
    assert mod.contract_schedule(path).run_interval_seconds == 7200
    assert parse_count[0] == 2


def test_the_shipped_contract_schedule_is_parsed_once_per_gate_tick(
    parse_count: list[int],
) -> None:
    mod.contract_schedule()
    before = parse_count[0]
    for _ in range(50):
        mod.contract_schedule()
    assert parse_count[0] == before
