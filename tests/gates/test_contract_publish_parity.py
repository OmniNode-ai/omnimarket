# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-vs-handler publish parity ratchet [OMN-17017, A10 step 1].

A contract that declares a publish topic its handler never publishes is worse
than a missing feature: every downstream reader — the CLI registry, the skill
docs, an audit — treats the declaration as the system (2026-08-29 beta
off-the-rails analysis rev 2, §RC-J).

The gate is a burn-down ratchet, not an allowlist: ``PARITY_DEBT`` freezes the
pre-existing offenders and may only shrink. A NEW offender, or growth of the
set, hard-fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_contract_publish_parity import (
    ModelPublishParityFinding,
    nodes_root,
    scan_publish_parity,
)
from scripts.ci.contract_publish_parity_baseline import PARITY_DEBT

_REPAIRED_NODES = (
    "node_wave_scheduler_orchestrator",
    "node_dispatch_watchdog_orchestrator",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_no_new_publish_parity_offenders() -> None:
    findings = scan_publish_parity(nodes_root(_repo_root()))
    new = sorted({f.node for f in findings} - set(PARITY_DEBT))

    assert not new, (
        "contract declares publish topics with no matching publish/emit call in "
        f"the node's handler: {new}. Either publish the event or delete the "
        "declaration from contract.yaml (OMN-17017 / A10 step 1)."
    )


@pytest.mark.unit
def test_baseline_only_shrinks() -> None:
    findings = {f.node for f in scan_publish_parity(nodes_root(_repo_root()))}
    stale = sorted(set(PARITY_DEBT) - findings)

    assert not stale, (
        "these nodes were repaired but are still frozen in "
        f"contract_publish_parity_baseline.PARITY_DEBT: {stale}. Remove them — "
        "the baseline is a burn-down list, not an allowlist."
    )


@pytest.mark.unit
@pytest.mark.parametrize("node", _REPAIRED_NODES)
def test_repaired_nodes_are_not_in_the_debt_baseline(node: str) -> None:
    assert node not in PARITY_DEBT


@pytest.mark.unit
@pytest.mark.parametrize("node", _REPAIRED_NODES)
def test_repaired_nodes_declare_only_publishable_topics(node: str) -> None:
    findings = scan_publish_parity(nodes_root(_repo_root()))

    assert not [f for f in findings if f.node == node], (
        f"{node} still declares an unpublished topic"
    )


@pytest.mark.unit
def test_gate_is_red_on_the_pre_repair_wave_scheduler(tmp_path: Path) -> None:
    """RED-first proof: the checker fails on the contract as it stood on dev."""
    node_dir = tmp_path / "node_wave_scheduler_orchestrator"
    (node_dir / "handlers").mkdir(parents=True)
    (node_dir / "handlers" / "handler_wave_scheduler_orchestrator.py").write_text(
        "class HandlerWaveSchedulerOrchestrator:\n    pass\n", encoding="utf-8"
    )
    (node_dir / "contract.yaml").write_text(
        "\n".join(
            [
                "name: node_wave_scheduler_orchestrator",
                "terminal_event: onex.evt.omnimarket.wave-scheduler-completed.v1",
                "event_bus:",
                "  publish_topics:",
                "    - onex.evt.omnimarket.wave-scheduler-wave-dispatched.v1",
                "    - onex.evt.omnimarket.wave-scheduler-wave-completed.v1",
                "    - onex.evt.omnimarket.wave-scheduler-completed.v1",
                "    - onex.evt.omnimarket.wave-scheduler-stall-detected.v1",
                "    - onex.evt.omnimarket.wave-scheduler-dependency-violation.v1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    findings = scan_publish_parity(tmp_path)

    assert findings == [
        ModelPublishParityFinding(
            node="node_wave_scheduler_orchestrator",
            undeclared_topics=(
                "onex.evt.omnimarket.wave-scheduler-dependency-violation.v1",
                "onex.evt.omnimarket.wave-scheduler-stall-detected.v1",
                "onex.evt.omnimarket.wave-scheduler-wave-completed.v1",
                "onex.evt.omnimarket.wave-scheduler-wave-dispatched.v1",
            ),
        )
    ]


@pytest.mark.unit
def test_terminal_event_is_exempt(tmp_path: Path) -> None:
    """The runtime publishes ``terminal_event``; the handler never does."""
    node_dir = tmp_path / "node_example_effect"
    (node_dir / "handlers").mkdir(parents=True)
    (node_dir / "handlers" / "handler_example.py").write_text(
        "class HandlerExample:\n    pass\n", encoding="utf-8"
    )
    (node_dir / "contract.yaml").write_text(
        "\n".join(
            [
                "name: node_example_effect",
                "terminal_event: onex.evt.omnimarket.example-completed.v1",
                "event_bus:",
                "  publish_topics:",
                "    - onex.evt.omnimarket.example-completed.v1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert scan_publish_parity(tmp_path) == []
