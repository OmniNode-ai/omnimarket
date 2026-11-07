# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the bring-up run over the host transport (OMN-19130).

The run handler drives the real shell probe and actuator; the shell is the
scripted adapter. Golden: a declared directory step reads not ok, its apply runs
through the fenced lease, its re-read is ok, one step event and one completed
terminal reach the bus. Error: h202's committed steps with the transport down
read ssh-config not ok; its action has no declared apply, so the run ends failed
naming ssh-config, with one failed terminal and the lease free.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.handler_host_shell_scripted import (
    DOWN,
    NOT_OK,
    OK,
    HandlerHostShellScripted,
)
from omnimarket.models.host_bringup.model_host_shell import ModelHostShellOverlay
from omnimarket.nodes.node_host_bringup_orchestrator.handlers.handler_host_bringup_run import (
    HandlerHostBringupRun,
)
from omnimarket.nodes.node_host_bringup_orchestrator.handlers.handler_shell_target_backends import (
    HandlerShellTargetActuator,
    HandlerShellTargetProbe,
    declared_subgraph,
    load_host_shell_overlay,
)
from omnimarket.nodes.node_host_bringup_orchestrator.handlers.inmemory_bringup_backends import (
    InMemoryBringupBus,
    InMemoryBringupLease,
    InMemoryClock,
)
from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_bringup_run import (
    EnumRunOutcome,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
)

pytestmark = pytest.mark.unit

_GRAPH = load_bringup_graph(
    yaml.safe_load(
        (
            Path(__file__).resolve().parent.parent
            / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
        ).read_text()
    )
)


def _run(
    host: str, overlay: ModelHostShellOverlay, shell: HandlerHostShellScripted
) -> tuple[ModelHostBringupRunResult, InMemoryBringupBus, InMemoryBringupLease]:
    entry = next(h for h in overlay.hosts if h.host == host)
    graph = declared_subgraph(_GRAPH, [s.target for s in entry.steps], "declared-up")
    clock = InMemoryClock()
    bus = InMemoryBringupBus()
    lease = InMemoryBringupLease(clock=clock)
    run = HandlerHostBringupRun(
        probe=HandlerShellTargetProbe(shell=shell, overlay=overlay),
        actuator=HandlerShellTargetActuator(shell=shell, overlay=overlay),
        lease=lease,
        bus=bus,
        clock=clock,
    )
    result = run.handle(
        ModelHostBringupRunRequest(
            host=host,
            run_id=f"run-{host}",
            graph=graph,
            top_target="declared-up",
            lease_grace_s=300,
            action_timeout_s=120,
            wait_timeout_s=0,
            poll_interval_s=5,
            max_passes=20,
            no_wait=True,
        )
    )
    return result, bus, lease


def test_golden_a_declared_apply_runs_and_the_run_completes() -> None:
    overlay = ModelHostShellOverlay.model_validate(
        {
            "ssh_options": ["StrictHostKeyChecking=yes"],
            "connect_timeout_s": 5,
            "probe_timeout_s": 10,
            "action_timeout_s": 20,
            "hosts": [
                {
                    "host": "h202",
                    "transport": {"kind": "ssh", "target": "h202"},
                    "steps": [
                        {"target": "key-login", "primitive": "transport_answers"},
                        {
                            "target": "lab-dirs",
                            "primitive": "directory",
                            "path": "/srv/lab",
                            "mode": "0755",
                        },
                    ],
                }
            ],
        }
    )
    shell = HandlerHostShellScripted(
        answers={
            "key-login probe": [OK],
            "lab-dirs probe": [NOT_OK],
            "lab-dirs act": [OK],
        },
        after_act={"lab-dirs": [OK]},
    )
    result, bus, lease = _run("h202", overlay, shell)
    assert result.outcome is EnumRunOutcome.COMPLETED
    assert result.exit_code == 0
    assert [(s.target, s.before, s.after) for s in bus.steps] == [
        ("lab-dirs", EnumObservedState.NOT_OK, EnumObservedState.OK)
    ]
    assert [t.outcome for t in bus.terminals] == [EnumRunOutcome.COMPLETED]
    assert lease.holder is None
    acts = [c for c in shell.calls if c[1].startswith("# onex-step lab-dirs act")]
    assert len(acts) == 1
    assert "install -d -m 0755 /srv/lab" in acts[0][1]


def test_error_h202_with_the_transport_down_fails_at_ssh_config() -> None:
    shell = HandlerHostShellScripted(
        answers={
            "ssh-config probe": [DOWN],
            "key-login probe": [DOWN],
            "root-access probe": [DOWN],
        }
    )
    result, bus, lease = _run("h202", load_host_shell_overlay(), shell)
    assert result.outcome is EnumRunOutcome.FAILED
    assert result.exit_code == 1
    assert result.failed_target == "ssh-config"
    assert "only reads" in result.detail
    assert bus.steps == []
    assert [t.outcome for t in bus.terminals] == [EnumRunOutcome.FAILED]
    assert lease.holder is None
    assert not any(c[1].split("\n", 1)[0].endswith(" act") for c in shell.calls)


def test_a_second_run_over_a_converged_host_acts_on_nothing_and_reads_every_step() -> (
    None
):
    overlay = load_host_shell_overlay()
    entry = next(h for h in overlay.hosts if h.host == "h053")
    keys = [f"{s.target} probe" for s in entry.steps]
    for _ in range(2):
        shell = HandlerHostShellScripted(answers={k: [OK] for k in keys})
        result, bus, lease = _run("h053", overlay, shell)
        assert result.exit_code == 0
        assert bus.steps == []
        probed = {c[1].split("\n", 1)[0] for c in shell.calls}
        assert probed == {f"# onex-step {k}" for k in keys}
        assert lease.holder is None
