# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20686: what the onex-ledger wrapper does with an argv, contract to bus to typed result.

Parity: tests/fixtures/onex_ledger_admission_parity.json holds cases captured by running the retired
onex_ledger.py end to end (a fake uv on PATH that records the command it is given, fake
omnibase_internal clones, an ONEX_LEDGER_BUDGET_S, TERMINAL rows in argv and in append-rows files),
beside the facts a caller read from the same fixtures. The node's action, exit code, stdout, stderr and
project must equal what the retired wrapper did. Golden chain: the request through the registered contract
on the in-memory bus. Error chain: a malformed request is refused with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_onex_ledger_admission_compute as node_package
from omnimarket.nodes.node_onex_ledger_admission_compute.handlers.handler_onex_ledger_admission import (
    HandlerOnexLedgerAdmission,
)
from omnimarket.nodes.node_onex_ledger_admission_compute.models.model_onex_ledger_admission import (
    ModelOnexLedgerAdmissionRequest,
    ModelOnexLedgerAdmissionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.onex-ledger-admission-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.onex-ledger-admission-completed.v1"
PARITY = json.loads(
    (ROOT / "tests/fixtures/onex_ledger_admission_parity.json").read_text()
)
HANDLER = HandlerOnexLedgerAdmission()
DEFAULT = "/omni/omnibase_internal"
GOOD_TERMINAL = "2026-10-09T10:00:00Z | TERMINAL | lane=x | actor=claude:opus | delegated=1 runs=abc | friction=none"


def request(**overrides: Any) -> dict[str, Any]:
    return {
        "argv": ["print-grammar"],
        "omni_home": "/omni/omni_home",
        "default_resolved": DEFAULT,
        "project_names": {DEFAULT: "omnibase-internal"},
        **overrides,
    }


def decide(**overrides: Any) -> ModelOnexLedgerAdmissionResult:
    return HANDLER.handle(
        ModelOnexLedgerAdmissionRequest.model_validate(request(**overrides))
    )


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeOnexLedgerAdmissionCompute, handler_type)
    for side, model in (
        ("input_model", ModelOnexLedgerAdmissionRequest),
        ("output_model", ModelOnexLedgerAdmissionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_onex_ledger_admission_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: str(c["name"]))
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    result = HANDLER.handle(
        ModelOnexLedgerAdmissionRequest.model_validate(case["request"])
    )
    assert result.model_dump(exclude_none=True) == case["expected"]


def test_parity_fixture_exercises_every_outcome() -> None:
    actions = {c["expected"]["action"] for c in PARITY["cases"]}
    assert actions == {"run", "refuse", "usage-error", "no-project"}
    stdout = [
        line for c in PARITY["cases"] for line in c["expected"].get("stdout_lines", [])
    ]
    for needle in (
        "REFUSED 65 delegation-cells: TERMINAL carries no delegated= cell",
        "REFUSED 65 delegation-cells: delegated=",
        "names no runs=",
        "needs delegation_reason=",
        "closes a lane only with outcome=rejected-no-delegation",
        "TERMINAL: ",
        "FIX: run the lane's delegation step",
    ):
        assert any(needle in line for line in stdout), needle
    stderr = [
        line for c in PARITY["cases"] for line in c["expected"].get("stderr_lines", [])
    ]
    assert any("must be a positive number of seconds" in line for line in stderr)
    assert any("no valid omnibase_internal clone" in line for line in stderr)
    assert any(c["expected"].get("budget_s") for c in PARITY["cases"])


def test_a_plain_argv_runs_in_the_default_clone() -> None:
    result = decide()
    assert (result.action, result.project, result.exit_code) == ("run", DEFAULT, None)
    assert result.budget_s is None
    assert result.timeout_stdout is None


def test_the_explicit_override_wins_and_never_falls_through() -> None:
    other = "/elsewhere/omnibase_internal"
    chosen = decide(
        path_override=other,
        override_resolved=other,
        project_names={DEFAULT: "omnibase-internal", other: "omnibase-internal"},
    )
    assert chosen.project == other
    refused = decide(
        path_override=other,
        override_resolved=other,
        project_names={DEFAULT: "omnibase-internal", other: "something-else"},
    )
    assert (refused.action, refused.exit_code, refused.project) == (
        "no-project",
        2,
        None,
    )
    assert refused.stderr_lines == [
        "onex-ledger resolver: no valid omnibase_internal clone. "
        f"OMNIBASE_INTERNAL_PATH tried: {other}; "
        f"$OMNI_HOME/../omnibase_internal tried: {DEFAULT} (not selected because OMNIBASE_INTERNAL_PATH is set). "
        'Expected a directory containing pyproject.toml with [project] name = "omnibase-internal".'
    ]


def test_no_omni_home_and_no_override_names_both_sources() -> None:
    result = decide(omni_home="", default_resolved=None, project_names={})
    assert result.action == "no-project"
    assert (
        "OMNIBASE_INTERNAL_PATH tried: <unset>; $OMNI_HOME/../omnibase_internal tried: <unavailable: OMNI_HOME is unset>."
        in result.stderr_lines[0]
    )


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "nan", "inf", "1e999"])
def test_a_bad_budget_is_a_usage_error_before_anything_else(raw: str) -> None:
    result = decide(budget_raw=raw, argv=["row", "TERMINAL"])
    assert (result.action, result.exit_code) == ("usage-error", 64)
    assert result.stderr_lines == [
        f"onex-ledger resolver: ONEX_LEDGER_BUDGET_S must be a positive number of seconds, got {raw}"
    ]
    assert not result.stdout_lines


def test_a_budget_names_what_the_wrapper_prints_when_it_runs_out() -> None:
    result = decide(budget_raw="2.5", argv=["append-rows", "f"], append_rows_text="")
    assert result.budget_s == 2.5
    assert (
        result.timeout_stdout
        == "REFUSED 124 budget: onex-ledger append-rows exceeded 2.5s"
    )
    assert result.timeout_stderr == (
        "RETRY 124 budget: onex-ledger append-rows exceeded its 2.5s budget and was stopped; "
        "rows it printed OK for landed, the rest did not"
    )
    assert (
        decide(budget_raw="3", argv=[]).timeout_stdout
        == "REFUSED 124 budget: onex-ledger ? exceeded 3s"
    )


@pytest.mark.parametrize(
    ("kv", "refusal"),
    [
        ("", "TERMINAL carries no delegated= cell"),
        ("delegated=x", "delegated=x is not a count"),
        ("delegated=2", "delegated=2 names no runs= (or codex=/jev=) ids that back it"),
        ("delegated=0", "delegated=0 needs delegation_reason= from the declared set"),
        ("delegated=0 delegation_reason=because", "got 'because'"),
        (
            "delegated=0 delegation_reason=no-delegation-line",
            "closes a lane only with outcome=rejected-no-delegation",
        ),
    ],
)
def test_a_terminal_without_its_delegation_cells_is_refused_with_65(
    kv: str, refusal: str
) -> None:
    result = decide(argv=["row", "TERMINAL", "--kv", kv, "--actor", "claude:x"])
    assert (result.action, result.exit_code) == ("refuse", 65)
    assert result.stdout_lines[0].startswith("REFUSED 65 delegation-cells: ")
    assert refusal in result.stdout_lines[0]
    assert result.stdout_lines[1].startswith(
        "FIX: run the lane's delegation step and pass its cells"
    )


@pytest.mark.parametrize(
    "kv",
    [
        "delegated=1 runs=abc",
        "delegated=1,; codex=t1",
        "delegated=0 delegation_reason=no-text-or-code",
        "delegated=0 delegation_reason=route-refused:503",
        "delegated=0 delegation_reason=route-unavailable:run:1",
        "delegated=0 delegation_reason=read-only-lane",
        "delegation=na:read-only",
    ],
)
def test_a_terminal_with_its_delegation_cells_runs(kv: str) -> None:
    assert (
        decide(argv=["row", "TERMINAL", "--kv", kv, "--actor", "claude:x"]).action
        == "run"
    )


def test_machine_writers_and_other_row_types_are_exempt() -> None:
    assert decide(argv=["row", "TERMINAL", "--actor", "script:x"]).action == "run"
    assert decide(argv=["row", "TERMINAL", "--model", "none"]).action == "run"
    assert decide(argv=["row", "STATUS"]).action == "run"


def test_append_rows_and_the_legacy_append_form_read_their_terminal_rows() -> None:
    bad = "2026-10-09T10:00:00Z | TERMINAL | lane=x | actor=claude:opus | friction=none"
    text = f"{GOOD_TERMINAL}\n{bad}\n2026-10-09T10:00:01Z | STATUS | lane=x\n"
    appended = decide(argv=["append-rows", "rows.txt"], append_rows_text=text)
    assert appended.exit_code == 65
    assert appended.stdout_lines[0].startswith(
        "REFUSED 65 delegation-cells: 2026-10-09T10:00:00Z TERMINAL: TERMINAL carries no delegated= cell"
    )
    legacy = decide(argv=["ledger.md", "--append", bad, "--x"])
    assert legacy.exit_code == 65
    unreadable = decide(argv=["append-rows", "gone.txt"])
    assert unreadable.action == "run"
    assert (
        decide(argv=["append-rows", "rows.txt"], append_rows_text=GOOD_TERMINAL).action
        == "run"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "argv"),
        (request(argv="row"), "argv"),
        (request(budget_raw=5), "budget_raw"),
        (request(surprise=1), "surprise|Extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelOnexLedgerAdmissionRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_a_captured_case(
    tmp_path: Path,
) -> None:
    case = next(c for c in PARITY["cases"] if c["expected"]["action"] == "refuse")
    runtime = await _run(tmp_path, case["request"])
    result = runtime.handler_result
    assert isinstance(result, ModelOnexLedgerAdmissionResult)
    assert result.model_dump(exclude_none=True) == case["expected"]
    assert (
        ModelOnexLedgerAdmissionResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(request(argv="row")))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
