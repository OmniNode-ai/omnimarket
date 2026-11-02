# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handoff receipt falsifiers through the contract's bus runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.handlers.handler_pr_handoff_dod_receipt import (
    HandlerPrHandoffDodReceipt,
)
from omnimarket.nodes.node_pr_handoff_dod_receipt_effect.models.model_pr_handoff_dod_receipt_request import (
    ModelPrHandoffDodReceiptRequest,
)

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "src/omnimarket/nodes/node_pr_handoff_dod_receipt_effect"
FIXTURES = ROOT / "tests/fixtures/pr_handoff_dod_receipt"
HEAD = "a" * 40
TICKET = "OMN-21285"
pytestmark = pytest.mark.unit


def _receipt(**updates: object) -> dict[str, object]:
    data = json.loads((FIXTURES / "passing_at_head.json").read_text())
    data.update(updates)
    return data


def _write(root: Path, data: object, name: str = "item/check.yaml") -> Path:
    path = root / TICKET / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data) if path.suffix == ".json" else yaml.safe_dump(data)
    )
    return path


def _request(root: Path) -> ModelPrHandoffDodReceiptRequest:
    return ModelPrHandoffDodReceiptRequest(
        ticket=TICKET, head=HEAD, receipts_root=str(root)
    )


@pytest.mark.parametrize(
    "profile",
    ["inmemory", pytest.param("redpanda", marks=pytest.mark.kafka)],
)
@pytest.mark.parametrize(
    ("fixture", "expected"), [("missing_receipt", False), ("passing_at_head", True)]
)
async def test_receipt_at_head_bus_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    fixture: str,
    expected: bool,
) -> None:
    """The same missing/bound fixtures traverse both real transport bindings."""
    record = json.loads((FIXTURES / f"{fixture}.json").read_text())
    for receipt in record.get("receipts", [record]):
        _write(tmp_path, receipt)
    request = _request(tmp_path)
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json())
    profiles = yaml.safe_load((FIXTURES / "profiles.yaml").read_text())
    overrides = dict(profiles[profile]["backend_overrides"])
    binding = nullcontext()
    if profile == "redpanda":
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import declared_test_lane, resolve_test_bus_broker

        overrides["kafka_bootstrap"] = resolve_test_bus_broker()
        selection, transport = resolve_lane_binding(declared_test_lane())
        assert selection.bootstrap_servers == overrides["kafka_bootstrap"]
        if transport is not None:
            binding = bind_lane_client_transport(transport)
        else:
            # The credential resolver selected the lab's environment identity;
            # protocol and mechanism still come from the lane declaration.
            monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", selection.security_protocol)
            if selection.sasl_mechanism:
                monkeypatch.setenv("KAFKA_SASL_MECHANISM", selection.sasl_mechanism)
    runtime = RuntimeLocal(
        workflow_path=NODE / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{uuid4()}",
        backend_overrides=overrides,
        timeout=30,
    )
    with binding:
        await runtime.run_async()
    result = runtime.handler_result
    assert result is not None, runtime.last_error
    assert result.admitted is expected
    assert result.code == (0 if expected else 4)
    assert result.correlation_id == request.correlation_id
    assert runtime.exit_code == (0 if expected else 1)
    assert runtime._contract["terminal_event"] == (
        "onex.evt.omnimarket.pr-handoff-dod-receipt-completed.v1"
    )
    saved = json.loads((runtime.state_root / "workflow_result.json").read_text())
    terminal = saved["terminal_payload"]
    payload = terminal.get("payload", terminal)
    assert payload["correlation_id"] == str(request.correlation_id)
    assert payload["admitted"] is expected


@pytest.mark.parametrize(
    "updates",
    [
        {"commit_sha": "b" * 40},
        {"commit_sha": HEAD[:7]},
        {"ticket_id": "OMN-1"},
        {"ticket_id": None},
        {"status": "FAIL"},
        {"status": "PENDING"},
        {"status": "ADVISORY"},
        {"verifier": "node-dod-verify"},
        {"run_timestamp": None},
        {"probe_stdout": ""},
    ],
)
def test_refuses_receipt_that_does_not_verify_ticket_at_full_head(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    _write(tmp_path, _receipt(**updates))
    result = HandlerPrHandoffDodReceipt().handle(_request(tmp_path))
    assert not result.admitted
    assert result.code != 0


def test_current_failure_blocks_a_passing_item(tmp_path: Path) -> None:
    _write(tmp_path, _receipt())
    _write(tmp_path, _receipt(status="FAIL"), "other/check.yaml")
    result = HandlerPrHandoffDodReceipt().handle(_request(tmp_path))
    assert not result.admitted
    assert result.code == 4
    assert result.failing == ("other/check.yaml=FAIL",)


def test_latest_numeric_supersession_is_the_current_receipt(tmp_path: Path) -> None:
    _write(tmp_path, _receipt(status="FAIL"))
    _write(tmp_path, _receipt(status="FAIL"), "item/check.supersede.9.yaml")
    _write(tmp_path, _receipt(), "item/check.supersede.10.yaml")
    assert HandlerPrHandoffDodReceipt().handle(_request(tmp_path)).admitted
    _write(tmp_path, _receipt(status="FAIL"), "item/check.supersede.11.yaml")
    assert not HandlerPrHandoffDodReceipt().handle(_request(tmp_path)).admitted


@pytest.mark.parametrize(
    "text", ["[broken", "null", "status: PASS\ncommit_sha: " + HEAD]
)
def test_unreadable_receipt_fails_closed(tmp_path: Path, text: str) -> None:
    path = _write(tmp_path, _receipt())
    path.write_text(text)
    result = HandlerPrHandoffDodReceipt().handle(_request(tmp_path))
    assert not result.admitted
    assert result.code == 2


def test_unreadable_diagnostic_does_not_echo_receipt_contents(tmp_path: Path) -> None:
    _write(tmp_path, ["sensitive-probe-fixture"])
    result = HandlerPrHandoffDodReceipt().handle(_request(tmp_path))
    assert result.code == 2
    assert "sensitive-probe-fixture" not in result.line
    assert "check.yaml" in result.line


def test_summary_receipt_and_evidence_root_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, _receipt(), "dod_report.json")
    monkeypatch.setenv("ONEX_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.delenv("ONEX_DOD_RECEIPTS_DIR", raising=False)
    result = HandlerPrHandoffDodReceipt().handle(
        ModelPrHandoffDodReceiptRequest(ticket=TICKET, head=HEAD)
    )
    assert result.admitted


def test_explicit_receipt_overlay_precedes_ambient_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, _receipt())
    monkeypatch.setenv("ONEX_DOD_RECEIPTS_DIR", str(tmp_path / "absent"))
    assert HandlerPrHandoffDodReceipt().handle(_request(tmp_path)).admitted


def test_contract_drives_paths_status_and_bus_routing(tmp_path: Path) -> None:
    data = yaml.safe_load((NODE / "contract.yaml").read_text())
    data["receipt_policy"]["summary_name"] = "proof.json"
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(data))
    _write(tmp_path, _receipt(), "proof.json")
    assert (
        HandlerPrHandoffDodReceipt(contract_path=path)
        .handle(_request(tmp_path))
        .admitted
    )
    data["receipt_policy"]["passing_status"] = "FAIL"
    path.write_text(yaml.safe_dump(data))
    assert (
        not HandlerPrHandoffDodReceipt(contract_path=path)
        .handle(_request(tmp_path))
        .admitted
    )


@pytest.mark.parametrize(
    ("ticket", "head"), [("../escape", HEAD), (TICKET, ""), (TICKET, HEAD[:7])]
)
def test_invalid_subject_refuses_before_reading(
    tmp_path: Path, ticket: str, head: str
) -> None:
    result = HandlerPrHandoffDodReceipt().handle(
        ModelPrHandoffDodReceiptRequest(
            ticket=ticket, head=head, receipts_root=str(tmp_path)
        )
    )
    assert not result.admitted
    assert result.code == 4


def test_default_store_is_the_declared_runtime_evidence_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers.runtime_paths import runtime_path

    monkeypatch.delenv("ONEX_DOD_RECEIPTS_DIR", raising=False)
    monkeypatch.delenv("ONEX_EVIDENCE_ROOT", raising=False)
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    root = tmp_path / runtime_path("dod_evidence.home_subdir")
    _write(root, _receipt(), "dod_report.json")
    result = HandlerPrHandoffDodReceipt().handle(
        ModelPrHandoffDodReceiptRequest(ticket=TICKET, head=HEAD)
    )
    assert result.admitted
    assert result.root == str(root)


def test_no_declared_store_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "ONEX_DOD_RECEIPTS_DIR",
        "ONEX_EVIDENCE_ROOT",
        "ONEX_STATE_DIR",
        "ONEX_STATE_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    result = HandlerPrHandoffDodReceipt().handle(
        ModelPrHandoffDodReceiptRequest(ticket=TICKET, head=HEAD)
    )
    assert not result.admitted
    assert result.code == 2


def test_node_registration_and_regression_gate_are_wired() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["entry-points"]["onex.nodes"][NODE.name] == (
        "omnimarket.nodes." + NODE.name
    )
    hook_config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in hook_config["repos"] for hook in repo["hooks"]]
    hook = next(h for h in hooks if h["id"] == "pr-handoff-dod-receipt-regression")
    assert "pre-commit" in hook["stages"]
    assert hook["pass_filenames"] is False
    assert re.search(hook["files"], str(Path(__file__).resolve().relative_to(ROOT)))
    assert re.search(
        hook["files"], str(NODE.relative_to(ROOT) / "runtime_pr_handoff_dod_receipt.py")
    )
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    commands = [
        step.get("run")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert hook["entry"] in commands
    assert "tests/test_golden_chain_pr_handoff_dod_receipt_effect.py" in hook["entry"]


@pytest.mark.parametrize("present", [False, True])
def test_shell_callout_uses_the_contract_bus_runtime(
    tmp_path: Path, present: bool
) -> None:
    if present:
        _write(tmp_path, _receipt())
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_handoff_dod_receipt_effect.runtime_pr_handoff_dod_receipt",
            TICKET,
            HEAD,
            "--receipts-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == (0 if present else 4), completed.stderr
    assert HEAD in completed.stdout
    assert ("PASS" if present else "no passing") in completed.stdout
