# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill ports take their timeouts from the node contract (OMN-21623)."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.contract_block_loader import ContractBlockError
from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_status import (
    HandlerLabFillStatus,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillPortConfig,
    load_lab_fill_port_config,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_port_config import (
    CONFIG_KEY,
    CONTRACT_PATH,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import local_lab_fill_adapters
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalLiveChecks,
    LocalOwnerReader,
)


def _block() -> dict[str, object]:
    block = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))[CONFIG_KEY]
    assert isinstance(block, dict)
    return dict(block)


def _config_with(tmp_path: Path, block: dict[str, object]) -> ModelLabFillPortConfig:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({CONFIG_KEY: block}), encoding="utf-8")
    return load_lab_fill_port_config(path)


def test_shipped_contract_carries_the_timeouts_the_ports_used_to_hold() -> None:
    config = load_lab_fill_port_config()
    assert config.linear_query_timeout_seconds == 20.0
    assert config.triage_fences_timeout_seconds == 60
    assert config.lane_rules_block_timeout_seconds == 60
    assert config.claim_bridge_timeout_seconds == 60
    assert config.pr_claim_registry_timeout_seconds == 20
    assert config.status_append_timeout_seconds == 60.0


@pytest.mark.parametrize("missing", sorted(_block()))
def test_a_block_missing_any_key_fails_to_load(tmp_path: Path, missing: str) -> None:
    block = _block()
    del block[missing]
    with pytest.raises(ContractBlockError, match=missing):
        _config_with(tmp_path, block)


class _NoAppender:
    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        raise AssertionError("the append port is not exercised here")


class _NoWriter:
    def write(self, path: str, value: Mapping[str, object]) -> None:
        raise AssertionError("the result writer is not exercised here")


def test_the_status_handler_waits_as_long_as_the_contract_says() -> None:
    handler = HandlerLabFillStatus(appender=_NoAppender(), writer=_NoWriter())
    assert (
        handler._timeout_s == load_lab_fill_port_config().status_append_timeout_seconds
    )
    explicit = HandlerLabFillStatus(
        appender=_NoAppender(), writer=_NoWriter(), timeout_s=5.0
    )
    assert explicit._timeout_s == 5.0


def test_pr_claim_registry_is_bounded_by_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with(
        tmp_path, {**_block(), "pr_claim_registry_timeout_seconds": 3}
    )
    seen: list[object] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    LocalOwnerReader(config=config).pr_claims("/x/cli.py")
    assert seen == [3]


def test_linear_query_and_fences_are_bounded_by_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with(
        tmp_path,
        {
            **_block(),
            "linear_query_timeout_seconds": 4.5,
            "triage_fences_timeout_seconds": 9,
        },
    )
    bounds: list[tuple[str, float]] = []

    def fake_graphql(_query: str, timeout_s: float) -> dict[str, object]:
        bounds.append(("graphql", timeout_s))
        return {
            "issues": {
                "nodes": [
                    {
                        "identifier": "OMN-1",
                        "state": {"type": "started"},
                        "assignee": None,
                    }
                ]
            }
        }

    def fake_fences(_ledger: str, timeout_s: float) -> set[str]:
        bounds.append(("fences", timeout_s))
        return set()

    monkeypatch.setattr(local_lab_fill_adapters, "_graphql", fake_graphql)
    monkeypatch.setattr(LocalLiveChecks, "_fences", staticmethod(fake_fences))
    LocalLiveChecks(config).skip_reason(
        ticket="OMN-1", kind="other", pr="", operator_id="op", ledger_path="/L"
    )
    assert bounds == [("graphql", 4.5), ("fences", 9)]
