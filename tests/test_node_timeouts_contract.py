# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Nodes read their timeouts from their contract.yaml, with every value as before."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.models.model_node_timeouts import ModelNodeTimeouts
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.handlers.handler_decision_extraction import (
    HandlerDecisionExtraction,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.handlers.handler_extraction_grader import (
    HandlerExtractionGrader,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.handlers.handler_segmentation import (
    HandlerSegmentation,
)
from omnimarket.nodes.node_auto_merge_effect.handlers import handler_auto_merge_effect
from omnimarket.nodes.node_automation_liveness_deadman_effect.handlers.ledger_appender import (
    LedgerCommandAppender,
)
from omnimarket.nodes.node_automation_liveness_watchdog_effect.handlers.alert_channel_sender import (
    AlertChannelAlarmSender,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.host_command_runner import (
    SubprocessHostCommand,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.kafka_consumer_group_reader import (
    _TIMEOUT_SECONDS as OBSERVER_KAFKA_TIMEOUT,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_system_health import (
    _TIMEOUT_S as SYSTEM_HEALTH_TIMEOUT,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_canonical_clone_refresh_effect import (
    SYNC_TIMEOUT_SECONDS,
)
from omnimarket.nodes.node_captured_secret_store_effect.handlers.handler_captured_secret_store import (
    TIMEOUT_SECONDS as SECRET_STORE_TIMEOUT,
)

# node -> {declared name: the value the node's code held as a literal before it moved}
DECLARED: dict[str, dict[str, float]] = {
    "node_ab_compare_orchestrator": {
        "quality_check_timeout_seconds": 10,
        "llm_call_timeout_seconds": 120.0,
    },
    "node_antipattern_index_effect": {"embedding_request_timeout_seconds": 30.0},
    "node_antipattern_match_effect": {"embedding_request_timeout_seconds": 30.0},
    "node_architecture_graph_populate_effect": {
        "retry_initial_backoff_seconds": 0.1,
        "retry_backoff_multiplier": 2.0,
    },
    "node_auto_merge_effect": {"poll_interval_s": 60},
    "node_automation_liveness_deadman_effect": {
        "ledger_command_timeout_seconds": 60,
        "sqlite_lock_timeout_seconds": 30,
    },
    "node_automation_liveness_watchdog_compute": {
        "selftest_interval_seconds": 600,
        "selftest_alarm_deadline_seconds": 86400,
    },
    "node_automation_liveness_watchdog_effect": {
        "alert_command_timeout_seconds": 30,
        "sqlite_lock_timeout_seconds": 30,
    },
    "node_automation_run_observer_effect": {
        "host_command_timeout_seconds": 20.0,
        "kafka_request_timeout_seconds": 15.0,
    },
    "node_baseline_capture": {
        "db_connect_timeout_seconds": 5.0,
        "github_prs_timeout_seconds": 30,
        "kafka_topics_timeout_seconds": 5.0,
        "linear_tickets_timeout_seconds": 10.0,
        "system_health_timeout_seconds": 3.0,
        "git_branch_age_timeout_seconds": 10,
        "git_branch_name_timeout_seconds": 10,
    },
    "node_beacon_trace_capture_effect": {
        "readback_poll_interval_seconds": 5,
        "readback_deadline_seconds": 60,
    },
    "node_canonical_clone_refresh_effect": {"sync_timeout_seconds": 300},
    "node_captured_secret_store_effect": {
        "store_call_timeout_seconds": 5.0,
        "credential_close_timeout_seconds": 30.0,
    },
}


@pytest.mark.unit
def test_slice_has_no_literal_timeout_findings() -> None:
    nodes = (
        *DECLARED,
        "node_adr_decision_extraction_llm_effect",
        "node_adr_extraction_grader_llm_effect",
        "node_adr_segmentation_llm_effect",
    )
    node_root = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
    paths = [node_root / name for name in nodes]
    assert all(path.is_dir() for path in paths), "a target node is missing"
    result = ValidatorContractsAndDi().validate(paths)
    assert result.metadata is not None, "the validator returned no scan metadata"
    processed = result.metadata.files_processed
    assert processed is not None, "the validator returned no file count"
    assert processed >= len(paths), "the validator did not scan the targets"
    findings = [
        issue for issue in result.issues if issue.rule_name == "literal_timeout"
    ]
    assert not findings, findings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("node_name", "declared"), sorted(DECLARED.items()), ids=sorted(DECLARED)
)
def test_contract_declares_every_duration_with_its_former_value(
    node_name: str, declared: dict[str, float]
) -> None:
    timeouts = load_node_timeouts(node_name)
    assert dict(timeouts.seconds) == declared
    for name, value in declared.items():
        assert timeouts.get(name) == value


@pytest.mark.unit
def test_undeclared_name_stops_the_node() -> None:
    with pytest.raises(KeyError, match="declares no timeout named 'nope'"):
        ModelNodeTimeouts(seconds={"a": 1.0}).get("nope")


@pytest.mark.unit
def test_contract_without_a_timeouts_section_refuses() -> None:
    with pytest.raises(KeyError, match="timeouts"):
        load_node_timeouts("node_lane_model_policy_compute")


@pytest.mark.unit
def test_module_constants_come_from_the_contract() -> None:
    assert handler_auto_merge_effect._POLL_INTERVAL_S == 60
    assert OBSERVER_KAFKA_TIMEOUT == 15.0
    assert SYSTEM_HEALTH_TIMEOUT == 3.0
    assert SYNC_TIMEOUT_SECONDS == 300
    assert SECRET_STORE_TIMEOUT == 5.0


@pytest.mark.unit
def test_constructor_defaults_come_from_the_contract() -> None:
    assert HandlerDecisionExtraction()._extraction_timeout_seconds == 120.0
    assert HandlerExtractionGrader()._grader_timeout_seconds == 120.0
    assert HandlerSegmentation()._timeout == 120.0
    assert SubprocessHostCommand()._timeout == 20.0
    assert AlertChannelAlarmSender(Path("lib"))._timeout == 30
    assert LedgerCommandAppender(Path("ledger"), ("append",))._timeout == 60


@pytest.mark.unit
def test_an_injected_duration_wins_over_the_contract() -> None:
    assert SubprocessHostCommand(timeout_seconds=1.5)._timeout == 1.5
    assert AlertChannelAlarmSender(Path("lib"), timeout_seconds=2)._timeout == 2
    assert (
        LedgerCommandAppender(Path("ledger"), ("append",), timeout_seconds=3)._timeout
        == 3
    )
    assert (
        HandlerDecisionExtraction(
            extraction_timeout_seconds=4.0
        )._extraction_timeout_seconds
        == 4.0
    )
