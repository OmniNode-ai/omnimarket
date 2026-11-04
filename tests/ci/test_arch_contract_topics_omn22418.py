# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ARCH-TOPIC-002 checks YAML topic declarations, not entry-point prose."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/ci/check_arch_contract_topics.py"
_SPEC = importlib.util.spec_from_file_location("arch_contract_topics", _PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_CHECK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHECK)


def test_entry_point_prose_is_not_a_topic() -> None:
    assert (
        _CHECK.violations(
            {"dod_evidence": ["Registered under [project.entry-points.'onex.nodes']"]}
        )
        == []
    )


def test_valid_root_and_nested_topics_pass() -> None:
    assert (
        _CHECK.violations(
            {
                "publish_topics": [
                    {"topic": "onex.evt.omnimarket.ticket-query-completed.v1"}
                ],
                "event_bus": {
                    "subscribe_topics": ["onex.cmd.omnimarket.ticket-query.v1"]
                },
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "topic",
    [
        "onex.nodes",
        "onex.bad.producer.name.v1",
        "onex.evt.good.name.v1.trailing",
        "bad-topic",
    ],
)
def test_invalid_topics_fail_even_beside_a_valid_topic(topic: str) -> None:
    assert _CHECK.violations({"publish_topics": [{"topic": topic}]})
    assert _CHECK.violations(
        {"event_bus": {"subscribe_topics": ["onex.cmd.good.valid.v1", topic]}}
    )
    assert _CHECK.violations({"handler_routing": {"handlers": [{"topic": topic}]}})


def test_unreadable_yaml_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text("publish_topics: [")
    assert _CHECK.main([str(path)]) == 1
    assert "cannot read contract" in capsys.readouterr().out
