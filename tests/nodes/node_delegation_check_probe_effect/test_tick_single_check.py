# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tick runs one named check and tells a failed check from one it could not measure (OMN-26043).

The build checks node runs the tick once per check. Its executor reads exit 0 as pass, 1 as fail and anything else as
unavailable, so a check that read NOT_MEASURED (a lab model that never answered) or PRECONDITION-ABSENT (no collector) must
not exit 1: that would read as a regression of the product.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _gateway_fake import FakeGateway

from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    contract_topics,
)


def _overlays(tmp: Path, checks: list[str]) -> dict[str, str]:
    probe = tmp / "probe.yaml"
    probe.write_text(
        yaml.safe_dump(
            {
                "checks": checks,
                "history_file": "runs.jsonl",
                "history_keep": 5,
                "result_topic": contract_topics()[0],
                "responder_timeout_s": 1,
            }
        )
    )
    row = tmp / "responders.yaml"
    row.write_text(
        yaml.safe_dump({"schema_version": 1, "responders": [{"tier": "openrouter"}]})
    )
    return {tick.OVERLAY_FILE_ENV: str(probe), tick.RESPONDERS_FILE_ENV: str(row)}


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[int, InMemoryCheckPublisher]:
    for key, value in _overlays(tmp_path, ["C13", "C14", "C17"]).items():
        monkeypatch.setenv(key, value)
    pub = InMemoryCheckPublisher()
    code = tick.main(
        [
            "--omni-home",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            *extra,
        ],
        publisher=pub,
        gateway=FakeGateway(),
    )
    return code, pub


def test_check_runs_only_the_named_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, pub = _run(tmp_path, monkeypatch, "--check", "C14")
    assert [e["payload"]["criterion"] for e in pub.events] == ["C14"]
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert [r["check"] for r in evidence["runs"]] == ["C14"]


def test_a_check_that_could_not_be_measured_does_not_exit_as_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chain here has no local responder, so the run reads NOT_MEASURED and the unit's own exit stays 1.
    code, pub = _run(tmp_path, monkeypatch, "--check", "C13")
    assert pub.events[0]["payload"]["verdict"] == "NOT_MEASURED"
    assert code == tick.EXIT_NOT_MEASURED
    assert code not in (0, tick.EXIT_FAILED)


def test_the_timer_run_with_no_check_flag_keeps_its_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, pub = _run(tmp_path, monkeypatch)
    assert [e["payload"]["criterion"] for e in pub.events] == ["C13", "C14", "C17"]
    assert code == tick.EXIT_FAILED


def test_a_tick_with_no_responders_row_still_runs_c17_and_publishes_c13_and_c14_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings store serves no delegation_check_responders row (measured on h053, 2026-11-25): the tick ended at once
    with overlay_unreadable, so no check published a result. A missing row is an absent instrument for the two checks that
    send a task through the chain; C17 reads no responder and still grades."""
    from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
        InMemoryCustomerMachine,
    )
    from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
        ModelBrokenCommandCase,
        ModelSurfaceObservation,
    )

    env = _overlays(tmp_path, ["C13", "C14", "C17"])
    monkeypatch.setenv(tick.OVERLAY_FILE_ENV, env[tick.OVERLAY_FILE_ENV])
    monkeypatch.delenv(tick.RESPONDERS_FILE_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    surface = ModelSurfaceObservation(
        guides_lead_with_local=True,
        install_rc=0,
        install_interpreter_matches=True,
        help_answers=True,
        parameter_controls_caught=True,
        operator_switch_env="ONEX_OPERATOR_SWITCH",
        broken_ran_with_switch=True,
        broken_before_rc=0,
        broken_after_rc=0,
        broken_cases=(
            ModelBrokenCommandCase(
                entry_point="c17-broken-probe-import",
                rc=1,
                output="[ONEX_CORE_124_IMPORT_ERROR] 'c17-broken-probe-import' failed",
            ),
        ),
    )
    pub = InMemoryCheckPublisher()
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=pub,
        gateway=FakeGateway(),
        machine=InMemoryCustomerMachine(surface=surface),
    )
    verdicts = {e["payload"]["criterion"]: e["payload"]["verdict"] for e in pub.events}
    assert verdicts["C13"] == "PRECONDITION-ABSENT"
    assert verdicts["C14"] == "PRECONDITION-ABSENT"
    assert verdicts["C17"] == "PASS"
    assert code != tick.EXIT_OVERLAY
