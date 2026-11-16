# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the contract's receipt assertion against accepted and misleading receipts."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
CONTRACT = Path(__file__).parents[3] / "contracts/OMN-24980.yaml"


def proof_command() -> str:
    data = yaml.safe_load(CONTRACT.read_text())
    item = next(
        i for i in data["dod_evidence"] if i["id"] == "dod-omn24980-integration-proof"
    )
    return str(item["checks"][0]["check_value"])


def validator() -> str:
    argv = shlex.split(proof_command())
    candidates = [
        argv[i + 2]
        for i, word in enumerate(argv[:-2])
        if word == "python3" and argv[i + 1] == "-c"
    ]
    assert candidates, "proof must parse the answering receipt, not grep candidates"
    return candidates[-1]


def receipt(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "bus": "kafka",
        "lane": "dev",
        "locus": "deployed-lane",
        "status": "success",
        "backend_id": "local-heavy-reasoning",
        "routing_tier": "local",
        "receipt": {
            "result": {
                "terminal_payload": {
                    "payload": {
                        "status": "completed",
                        "attempts": [
                            {
                                "backend_id": "local-heavy-reasoning",
                                "tier": "local",
                                "acceptance_decision": "accept",
                                "quality_gate_passed": True,
                            }
                        ],
                    }
                }
            }
        },
    }
    result.update(updates)
    return result


def check(
    tmp_path: Path, payload: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(payload))
    return subprocess.run(
        [sys.executable, "-c", validator(), str(path)],
        check=False,
        text=True,
        capture_output=True,
    )


def test_proof_accepts_the_dev_runtime_answer(tmp_path: Path) -> None:
    completed = check(tmp_path, receipt())
    assert completed.returncode == 0, completed.stderr
    command = shlex.split(proof_command())
    assert command[command.index("--bus") + 1] == "kafka"
    assert command[command.index("--lane") + 1] == "dev"
    assert command[command.index("--locus") + 1] == "deployed-lane"
    assert not any(
        word.startswith("DELEGATION_ROUTING_TIERS_PATH=") for word in command
    )


@pytest.mark.parametrize(
    "change",
    [
        {"locus": "in-process"},
        {"bus": "inmemory"},
        {"lane": "other"},
        {"status": "failed"},
        {"backend_id": "hosted-answer"},
        {"routing_tier": "cheap_cloud"},
    ],
)
def test_proof_refuses_other_execution_or_answer(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    assert check(tmp_path, receipt(**change)).returncode != 0


def test_proof_refuses_a_skipped_local_candidate_even_when_top_level_mentions_it(
    tmp_path: Path,
) -> None:
    payload = receipt()
    payload["receipt"] = {
        "result": {
            "terminal_payload": {
                "payload": {
                    "status": "completed",
                    "attempts": [
                        {
                            "backend_id": "local-heavy-reasoning",
                            "tier": "local",
                            "acceptance_decision": "skip",
                            "quality_gate_passed": False,
                        },
                        {
                            "backend_id": "hosted-answer",
                            "tier": "cheap_cloud",
                            "acceptance_decision": "accept",
                            "quality_gate_passed": True,
                        },
                    ],
                }
            }
        }
    }
    assert check(tmp_path, payload).returncode != 0


def test_contract_does_not_bind_ceiling_tests_to_missing_terminal_cause() -> None:
    data = yaml.safe_load(CONTRACT.read_text())
    for item in data["dod_evidence"]:
        if "ceiling" in item["id"]:
            assert "AC1" not in item.get("binds_ac", [])
    assert "AC1: OPEN" in data["summary"]
    assert "AC2: OPEN" in data["summary"]
    assert "AC3: OPEN" in data["summary"]
