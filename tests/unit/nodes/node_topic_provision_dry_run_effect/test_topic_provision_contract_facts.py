# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for read_contract_facts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.contract_facts import (
    read_contract_facts,
)

pytestmark = pytest.mark.unit


def _write(root: Path, name: str, data: object) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "contract.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_schema_a_flat_string_lists(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "alpha",
        {
            "contract_name": "alpha",
            "runtime_profiles": ["main"],
            "event_bus": {
                "subscribe_topics": ["t1", "t2"],
                "publish_topics": ["t3"],
                "dlq_topics": ["t4"],
            },
        },
    )
    facts = read_contract_facts([tmp_path])
    assert len(facts) == 1
    assert facts[0].contract_name == "alpha"
    assert facts[0].runtime_profiles == ("main",)
    assert set(facts[0].topics) == {"t1", "t2", "t3", "t4"}


def test_schema_b_mapping_entries(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "beta",
        {
            "contract_name": "beta",
            "runtime_profiles": ["main"],
            "consumed_events": [{"topic": "c1"}],
            "published_events": [{"topic": "p1"}],
            "runtime_dispatch": {
                "command_topic": "cmd1",
                "terminal_events": {"success": "s1", "failure": "f1"},
            },
        },
    )
    facts = read_contract_facts([tmp_path])
    assert set(facts[0].topics) == {"c1", "p1", "cmd1", "s1", "f1"}


def test_runtime_profiles_union_of_top_and_descriptor(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "gamma",
        {
            "contract_name": "gamma",
            "runtime_profiles": ["main"],
            "descriptor": {"runtime_profiles": ["effects"]},
        },
    )
    facts = read_contract_facts([tmp_path])
    assert set(facts[0].runtime_profiles) == {"main", "effects"}


def test_name_fallback_order(tmp_path: Path) -> None:
    _write(tmp_path, "dir_a", {"contract_name": "explicit"})
    _write(tmp_path, "dir_b", {"name": "named"})
    _write(tmp_path, "dir_c", {"foo": "bar"})
    facts = {f.contract_name for f in read_contract_facts([tmp_path])}
    assert facts == {"explicit", "named", "dir_c"}


def test_non_mapping_yaml_is_skipped(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "contract.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert read_contract_facts([tmp_path]) == ()


def test_two_contracts_sharing_a_name_are_both_returned(tmp_path: Path) -> None:
    _write(tmp_path, "one", {"contract_name": "shared"})
    _write(tmp_path, "two", {"contract_name": "shared"})
    facts = read_contract_facts([tmp_path])
    assert len(facts) == 2
    assert all(f.contract_name == "shared" for f in facts)
