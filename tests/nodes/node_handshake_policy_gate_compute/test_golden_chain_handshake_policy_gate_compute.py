# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20671: the handshake policy gate decisions, contract to bus to typed result, parity and refusals.

Parity: tests/fixtures/handshake_policy_gate_parity.json holds cases captured by running the
retired check-policy-gate.sh against a scripted `gh` (source commit recorded in the file).
Each case is replayed through the node by a driver that does what the script's loop did:
read, ask the node what the read means, sleep, repeat. The driver must reproduce the
script's stdout, stderr, exit code, endpoints read and sleeps asked for. Golden chain: the
registered contract on the in-memory bus. Error chain: every refusal is a refusal before the
handler runs.
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

import omnimarket.nodes.node_handshake_policy_gate_compute as node_package
from omnimarket.nodes.node_handshake_policy_gate_compute.handlers.handler_handshake_policy_gate import (
    HandlerHandshakePolicyGate,
)
from omnimarket.nodes.node_handshake_policy_gate_compute.models.model_handshake_policy_gate import (
    EnumPolicyGateVerdict,
    EnumReadOutcome,
    EnumRepoGateStatus,
    ModelPolicyGateDecisionRequest,
    ModelPolicyGateDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.handshake-policy-gate-decide-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.handshake-policy-gate-decided.v1"
PARITY = json.loads(
    (ROOT / "tests/fixtures/handshake_policy_gate_parity.json").read_text()
)
HANDLER = HandlerHandshakePolicyGate()


def decide(**fields: Any) -> ModelPolicyGateDecisionResult:
    return HANDLER.handle(ModelPolicyGateDecisionRequest.model_validate(fields))


def _scripted_read(body: str) -> dict[str, Any]:
    """What the caller sees for one scripted gh response: the fixture's own response grammar."""
    if body.startswith("ERR:"):
        return {"api_ok": False, "api_error_text": body[4:].rstrip("\n")}
    page = json.loads(body)
    runs = page["workflow_runs"]
    return {
        "api_ok": True,
        "total_count": page["total_count"],
        "conclusion": runs[0]["conclusion"] if runs else "",
    }


def replay(case: dict[str, Any]) -> dict[str, Any]:
    """Drive the node through the retired script's loop; return what the script produced."""
    env = case.get("env", {})
    stderr: list[str] = []
    endpoints: list[str] = []
    sleeps: list[int] = []
    try:
        parsed = decide(kind="parse_repos", repos_conf_text=case["conf"])
    except ValueError:
        return {
            "stdout": "",
            "stderr": "ERROR: repos.conf contains no repo entries\n",
            "exit_code": 2,
            "endpoints": [],
            "sleeps": [],
        }
    assert parsed.repos is not None
    assert parsed.info_line is not None
    stderr.append(parsed.info_line)
    statuses = []
    for repo in parsed.repos:
        spec = case["repos"][repo]
        branch_output = "" if spec["branch"].startswith("ERR:") else spec["branch"]
        resolved = decide(
            kind="resolve_branch", repo=repo, default_branch_output=branch_output
        )
        assert resolved.default_branch_endpoint
        assert resolved.runs_endpoint
        endpoints.append(resolved.default_branch_endpoint)
        if resolved.info_line:
            stderr.append(resolved.info_line)
        attempt = 1
        reads = spec["runs"]
        while True:
            endpoints.append(resolved.runs_endpoint)
            read = _scripted_read(reads[min(attempt, len(reads)) - 1])
            step = decide(
                kind="classify_read",
                repo=repo,
                attempt=attempt,
                max_attempts_raw=env.get("POLICY_GATE_RETRY_ATTEMPTS"),
                base_delay_raw=env.get("POLICY_GATE_RETRY_BASE_DELAY"),
                **read,
            )
            if step.outcome is EnumReadOutcome.RETRY:
                assert step.info_line
                assert step.retry_delay_seconds is not None
                stderr.append(step.info_line)
                sleeps.append(step.retry_delay_seconds)
                assert step.next_attempt == attempt + 1
                attempt = step.next_attempt
                continue
            assert step.outcome is not None
            statuses.append({"repo": repo, "status": step.outcome.value})
            break
    report = decide(kind="report", statuses=statuses, strict=case["strict"])
    return {
        "stdout": report.report_text,
        "stderr": "".join(f"{line}\n" for line in stderr),
        "exit_code": report.exit_code,
        "endpoints": endpoints,
        "sleeps": sleeps,
    }


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
    assert issubclass(node_package.NodeHandshakePolicyGateCompute, handler_type)
    for side, model in (
        ("input_model", ModelPolicyGateDecisionRequest),
        ("output_model", ModelPolicyGateDecisionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_handshake_policy_gate_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: c["name"])
def test_old_script_parity(case: dict[str, Any]) -> None:
    assert replay(case) == case["old"]


def test_parity_fixture_exercises_every_status_verdict_and_exit_code() -> None:
    stdout = "".join(c["old"]["stdout"] for c in PARITY["cases"])
    for needle in (
        "workflow passing",
        "workflow failing",
        "no check-handshake workflow found",
        "has no runs",
        "API error",
        "POLICY GATE: PASSED",
        "POLICY GATE: FAILED (strict mode)",
        "POLICY GATE: WARNING (report-only mode",
    ):
        assert needle in stdout
    assert {c["old"]["exit_code"] for c in PARITY["cases"]} == {0, 1, 2}
    assert any(c["old"]["sleeps"] for c in PARITY["cases"])


def test_zero_padded_base_delay_is_decimal() -> None:
    # The retired script read "08" as octal and died under set -e; the node reads decimal.
    step = decide(
        kind="classify_read",
        repo="r",
        api_ok=False,
        api_error_text="HTTP 500",
        base_delay_raw="08",
    )
    assert (step.outcome, step.retry_delay_seconds) == (EnumReadOutcome.RETRY, 8)


def test_report_lists_failures_in_check_order() -> None:
    result = decide(
        kind="report",
        statuses=[
            {"repo": "b", "status": "fail"},
            {"repo": "a", "status": "pass"},
            {"repo": "c", "status": "no_runs"},
        ],
        strict=True,
    )
    assert result.failed_repos == ["b", "c"]
    assert (result.pass_count, result.fail_count) == (1, 2)
    assert result.verdict is EnumPolicyGateVerdict.FAILED
    assert result.exit_code == 1


def test_report_only_mode_warns_and_exits_zero() -> None:
    result = decide(
        kind="report", statuses=[{"repo": "b", "status": "error"}], strict=False
    )
    assert result.verdict is EnumPolicyGateVerdict.WARNING
    assert result.exit_code == 0


def test_status_enum_matches_the_reads_the_report_can_show() -> None:
    assert {s.value for s in EnumRepoGateStatus} == {
        "pass",
        "fail",
        "no_workflow",
        "no_runs",
        "error",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "parse_repos"}, "parse_repos requires repos_conf_text"),
        ({"kind": "resolve_branch", "repo": "r"}, "requires default_branch_output"),
        ({"kind": "classify_read", "repo": "r"}, "classify_read requires api_ok"),
        ({"kind": "report"}, "report requires statuses"),
        ({"kind": "report", "statuses": []}, "at least 1"),
        (
            {"kind": "report", "statuses": [{"repo": "r", "status": "weird"}]},
            "status",
        ),
        (
            {"kind": "parse_repos", "repos_conf_text": "r", "strict": True},
            "parse_repos does not take strict",
        ),
        (
            {
                "kind": "report",
                "statuses": [{"repo": "r", "status": "pass"}],
                "attempt": 2,
            },
            "report does not take attempt",
        ),
        (
            {"kind": "classify_read", "repo": "r", "api_ok": True, "attempt": 0},
            "greater than or equal to 1",
        ),
        ({"kind": "bogus"}, "kind"),
        ({"kind": "parse_repos", "repos_conf_text": "r", "extra": 1}, "extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelPolicyGateDecisionRequest.model_validate(payload)


def test_empty_repos_conf_is_refused() -> None:
    with pytest.raises(ValueError, match="handshake-policy-gate"):
        decide(kind="parse_repos", repos_conf_text="# only a comment\n\n")


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
async def test_golden_chain_parse_then_classify_then_report_over_the_bus(
    tmp_path: Path,
) -> None:
    (tmp_path / "parse").mkdir()
    parsed = (
        await _run(
            tmp_path / "parse",
            {"kind": "parse_repos", "repos_conf_text": "alpha\n# skip\nbeta\n"},
        )
    ).handler_result
    assert isinstance(parsed, ModelPolicyGateDecisionResult)
    assert parsed.repos == ["alpha", "beta"]
    outcomes = []
    for index, read in enumerate(
        (
            {"api_ok": True, "total_count": 2, "conclusion": "success"},
            {"api_ok": False, "api_error_text": "HTTP 404: Not Found"},
        )
    ):
        (tmp_path / f"read{index}").mkdir()
        step = (
            await _run(
                tmp_path / f"read{index}",
                {"kind": "classify_read", "repo": parsed.repos[index], **read},
            )
        ).handler_result
        assert isinstance(step, ModelPolicyGateDecisionResult)
        assert step.outcome is not None
        outcomes.append(step.outcome.value)
    assert outcomes == ["pass", "no_workflow"]
    (tmp_path / "report").mkdir()
    done = (
        await _run(
            tmp_path / "report",
            {
                "kind": "report",
                "strict": True,
                "statuses": [
                    {"repo": r, "status": o}
                    for r, o in zip(parsed.repos, outcomes, strict=True)
                ],
            },
        )
    ).handler_result
    assert isinstance(done, ModelPolicyGateDecisionResult)
    assert (done.verdict, done.exit_code) == (EnumPolicyGateVerdict.FAILED, 1)
    assert done.failed_repos == ["beta"]
    assert (
        ModelPolicyGateDecisionResult.model_validate_json(done.model_dump_json())
        == done
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {"kind": "report"})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"kind": "parse_repos", "repos_conf_text": "# none\n"}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=empty,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
