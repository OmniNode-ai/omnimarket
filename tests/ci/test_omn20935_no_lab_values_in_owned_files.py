# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The omnimarket files whose lab values moved to the private overlay carry none (OMN-20935).

A lab address, tailnet name, lab host name or lab host shorthand in one of these
files is a deployment fact the shipped package must not carry (operator ruling
2026-10-10T21:57:38Z). Each file's value moved to the deployment's overlay or
environment; the reader refuses, or takes a neutral default, without it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The lab names are assembled from parts so this file carries none of them whole.
_LAN = ".".join(("192", "168", "86", "201"))
_TAILNET_HOST = "omninode" + "-pc." + "tail" + "75df5e" + ".ts.net"
_DATA_ROOT = "/data/" + "omninode"
_SHORTHAND = "." + "201"

#: A lab address or name.
LAB_ADDRESS = re.compile(
    "|".join(
        (
            r"192\.168\.\d{1,3}\.\d{1,3}",
            "tail" + "75df5e",
            "omninode" + "-pc",
            "sticky" + "beatz",
            re.escape(_DATA_ROOT),
        )
    )
)
#: A lab host shorthand, not a version or a longer dotted token.
LAB_SHORTHAND = re.compile(r"(?<![\w.])\.(?:200|201|202|101|105)(?![\w.])")

#: Files whose lab address and host shorthand moved out.
CLEAN_OF_ADDRESS_AND_SHORTHAND = (
    "benchmarks/delegation_false_pass_growth/mem_to_content.py",
    "experiments/adk_eval/type_debt_scout_poc/handler_type_debt_scout.py",
    "scripts/cost_event_publisher.py",
    "scripts/run_delegation_cost_projection_process.sh",
    "src/omnimarket/nodes/node_data_flow_sweep/lane_target.py",
    "src/omnimarket/nodes/node_database_sweep/handlers/handler_database_sweep.py",
    "src/omnimarket/nodes/node_delegation_orchestrator/models/model_delegation_escalation_attempt.py",
    "src/omnimarket/nodes/node_integration_sweep_orchestrator/models/model_integration_sweep_orchestrator_request.py",
    "src/omnimarket/nodes/node_integration_sweep_orchestrator/handlers/handler_integration_sweep_orchestrator.py",
    "src/omnimarket/nodes/node_runner_orchestrator/handlers/handler_runner_orchestrator.py",
    "src/omnimarket/nodes/node_runner_orchestrator/models/model_runner_result.py",
    "src/omnimarket/data/model_registry/model_registry_v1.yaml",
    "src/omnimarket/configs/routing_tiers.yaml",
)

#: Detector fixtures that deliberately carry a private address and host shorthand as
#: positive controls; they carry a synthetic address, never a lab one.
CLEAN_OF_ADDRESS_ONLY = (
    "src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_doc_content_scan.py",
    "src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_hardcoded_ip.py",
)

#: Files that left the shipped package.
REMOVED = (
    "src/omnimarket/nodes/node_swarm_registry_compute/contracts/endpoint_registry.yaml",
)


def _hits(path: str, *patterns: re.Pattern[str]) -> list[str]:
    text = (REPO / path).read_text(encoding="utf-8")
    return [
        f"{path}:{number}: {line.strip()[:100]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if any(pattern.search(line) for pattern in patterns)
    ]


@pytest.mark.parametrize("path", CLEAN_OF_ADDRESS_AND_SHORTHAND)
def test_file_carries_no_lab_address_or_host_shorthand(path: str) -> None:
    assert _hits(path, LAB_ADDRESS, LAB_SHORTHAND) == []


@pytest.mark.parametrize("path", CLEAN_OF_ADDRESS_ONLY)
def test_detector_fixture_carries_no_lab_address(path: str) -> None:
    assert _hits(path, LAB_ADDRESS) == []


@pytest.mark.parametrize("path", REMOVED)
def test_removed_file_is_absent(path: str) -> None:
    assert not (REPO / path).exists()


@pytest.mark.parametrize(
    "line",
    [
        f'HOST = "{_LAN}"',
        f"host: {_TAILNET_HOST}",
        f"ssh to the {_SHORTHAND} box",
        f"repo at {_DATA_ROOT}/checkout",
    ],
)
def test_the_patterns_flag_a_planted_lab_value(line: str) -> None:
    assert LAB_ADDRESS.search(line) or LAB_SHORTHAND.search(line)


@pytest.mark.parametrize(
    "line",
    [
        'HOST = "192.0.2.10"',
        "released as v1.201.0",
        "port 0.200 of the scale",
        "Qwen3.8-27B served locally",
    ],
)
def test_the_patterns_leave_documentation_ranges_and_versions_alone(line: str) -> None:
    assert LAB_ADDRESS.search(line) is None
    assert LAB_SHORTHAND.search(line) is None
