# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26152: a repeated contract read does not re-parse an unchanged file."""

from __future__ import annotations

import yaml

from omnimarket.nodes.node_dod_verify.handlers import dod_evidence_local_source as mod
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumRepoContractReadStatus,
)
from omnimarket.nodes.node_dod_verify.services.contract_parse_cache import (
    ContractParseCache,
)
from omnimarket.nodes.node_dod_verify.services.repo_contract_from_gateway import (
    repo_contract_from_gateway,
)

_TEXT = "ticket_id: OMN-1\ndod_evidence: []\n"


def _count_loads(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = yaml.safe_load

    def counting(text):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(text)

    monkeypatch.setattr(mod.yaml, "safe_load", counting)
    return calls


def test_second_read_of_unchanged_contract_parses_nothing(monkeypatch) -> None:
    calls = _count_loads(monkeypatch)
    cache = ContractParseCache()
    first = mod.parse_repo_contract(_TEXT, cache=cache, path="contracts/OMN-1.yaml")
    second = mod.parse_repo_contract(_TEXT, cache=cache, path="contracts/OMN-1.yaml")
    assert len(calls) == 1
    assert first == second
    assert first.status is EnumRepoContractReadStatus.FOUND


def test_changed_contract_is_reparsed(monkeypatch) -> None:
    calls = _count_loads(monkeypatch)
    cache = ContractParseCache()
    mod.parse_repo_contract(_TEXT, cache=cache, path="contracts/OMN-1.yaml")
    changed = mod.parse_repo_contract(
        _TEXT + "title: new\n", cache=cache, path="contracts/OMN-1.yaml"
    )
    assert len(calls) == 2
    assert changed.contract is not None
    assert changed.contract["title"] == "new"


def test_cached_read_cannot_be_corrupted_by_a_caller() -> None:
    cache = ContractParseCache()
    first = mod.parse_repo_contract(_TEXT, cache=cache, path="p")
    assert first.contract is not None
    first.contract["dod_evidence"] = ["mutated"]
    again = mod.parse_repo_contract(_TEXT, cache=cache, path="p")
    assert again.contract is not None
    assert again.contract["dod_evidence"] == []


def test_cache_is_bounded() -> None:
    cache = ContractParseCache(max_entries=2)
    for i in range(5):
        mod.parse_repo_contract(f"n: {i}\n", cache=cache, path=f"p{i}")
    assert len(cache) == 2


def test_errors_are_not_cached_wrongly(monkeypatch) -> None:
    calls = _count_loads(monkeypatch)
    cache = ContractParseCache()
    bad = mod.parse_repo_contract("- a\n- b\n", cache=cache, path="p")
    assert bad.status is EnumRepoContractReadStatus.ERROR
    mod.parse_repo_contract("- a\n- b\n", cache=cache, path="p")
    assert len(calls) == 1


def test_gateway_reader_uses_the_injected_cache(monkeypatch) -> None:
    from omnimarket.models.model_github_gateway_io import ModelRepoFileResult

    calls = _count_loads(monkeypatch)
    cache = ContractParseCache()
    read = ModelRepoFileResult.model_construct(text=_TEXT)
    repo_contract_from_gateway(read, cache=cache, path="p")
    repo_contract_from_gateway(read, cache=cache, path="p")
    assert len(calls) == 1


def test_unrelated_caches_do_not_share_state(monkeypatch) -> None:
    calls = _count_loads(monkeypatch)
    mod.parse_repo_contract(_TEXT, cache=ContractParseCache(), path="p")
    mod.parse_repo_contract(_TEXT, cache=ContractParseCache(), path="p")
    assert len(calls) == 2
