# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal_event seam of node_code_contract_score_effect (OMN-24334).

``onex run-node`` resolves the node's topics through
``omnibase_core.cli.cli_run_node._resolve_node_topics``; the top-level
``terminal_event`` key is the answer it waits for, and the failure answer stays
under ``runtime_dispatch.terminal_events.failure``. The golden test reads the committed
contract through that resolver; the error chain is a contract with the key removed; the
failing control is a contract whose ``terminal_event`` names another topic than its success
answer, which the agreement check refuses and names both topics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.cli import cli_run_node
from omnibase_core.errors import ModelOnexError

pytestmark = pytest.mark.unit

NODE_ID = "node_code_contract_score_effect"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_code_contract_score_effect/contract.yaml"
)

# Spelled out here, not read from the contract: a move on either side must fail this test.
COMMAND_TOPIC = "onex.cmd.omnimarket.code-contract-score-requested.v1"
RESPONSE_TOPIC = "onex.evt.omnimarket.code-contract-scored.v1"


def _contract_data(path: Path = _CONTRACT) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _success_topic(data: dict[str, object]) -> object:
    runtime_dispatch: object = data["runtime_dispatch"]
    assert isinstance(runtime_dispatch, dict)
    terminal_events: object = runtime_dispatch["terminal_events"]
    assert isinstance(terminal_events, dict)
    return terminal_events["success"]


def _resolve(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """The command and response topics run-node resolves from the contract at ``path``."""
    monkeypatch.setattr(cli_run_node, "_resolve_packaged_contract", lambda _node: path)
    (
        _contract_path,
        command_topic,
        response_topic,
        _inject_correlation_id,
    ) = cli_run_node._resolve_node_topics(NODE_ID)
    return command_topic, response_topic


def _require_terminal_event_agrees_with_success(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a contract whose run-node response topic is not its runtime_dispatch success topic."""
    _command, response_topic = _resolve(path, monkeypatch)
    success = _success_topic(_contract_data(path))
    if response_topic != success:
        raise AssertionError(
            f"terminal_event {response_topic} differs from runtime_dispatch.terminal_events.success {success}"
        )


def test_resolve_node_topics_names_the_command_and_response_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The committed contract resolves to the pinned topics, and the response topic is its success answer."""
    command_topic, response_topic = _resolve(_CONTRACT, monkeypatch)
    assert command_topic == COMMAND_TOPIC
    assert response_topic == RESPONSE_TOPIC
    _require_terminal_event_agrees_with_success(_CONTRACT, monkeypatch)


def test_removing_the_terminal_event_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract without the top-level terminal_event key raises a ModelOnexError naming it."""
    data = _contract_data()
    del data["terminal_event"]
    stripped = tmp_path / "contract.yaml"
    stripped.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ModelOnexError, match="terminal_event"):
        _resolve(stripped, monkeypatch)


def test_a_terminal_event_that_differs_from_the_success_topic_is_refused_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: the agreement check of the golden test refuses a drifted contract and names both topics."""
    other = "onex.evt.omnimarket.code-contract-scored-other.v1"
    data = _contract_data()
    data["terminal_event"] = other
    drifted = tmp_path / "contract.yaml"
    drifted.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(AssertionError) as refusal:
        _require_terminal_event_agrees_with_success(drifted, monkeypatch)
    assert other in str(refusal.value)
    assert RESPONSE_TOPIC in str(refusal.value)
