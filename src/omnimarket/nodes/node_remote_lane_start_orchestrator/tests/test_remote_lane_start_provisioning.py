# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start provisions the host in the remote-lane runner's order (OMN-23528).

Golden chain: the overlay's plan, run over the scripted transport and a ledger fixture, issues the fifteen
provisioning steps as host calls in the runner's order around the CLAIM, the engine and the closing row, and the
receipt holds each step's result. Error chain: a fatal step that fails ends the start with a closing row and no
engine; a step that is not fatal is recorded and the lane goes on. The policy (order, phase, fatal or not) is read
from the overlay, so flipping a value flips the behaviour: the failing controls plant that flip.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_spi.contracts.services import ModelLanePluginTree
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    STEP_NOT_RUN_BY_NODE,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.handler_remote_lane_start import (
    build_real,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_provisioning import (
    ModelLaneProvisioningPlan,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    Rig,
    cells,
    request,
    settings,
    snapshot,
    stream_result,
)

pytestmark = [pytest.mark.unit]

EXAMPLE = Path(__file__).resolve().parents[1] / "overlay.example.yaml"

#: The runner's order and policy, read from onex_remote_lane.py on 2026-11-10 (the function that issues each
#: step in ``run_lab``/``close_remote``/``retain_remote``): (step, fatal). A fatal step ends the start (the runner
#: ``finish``es with a refusal status); the others are recorded and echoed. The node writes its CLAIM before the
#: first of these (the spi protocol issues the steps after the CLAIM), which is the one move from the runner's order.
RUNNER_ORDER: tuple[tuple[str, bool], ...] = (
    ("resolve_registry_root", True),  # omni_home_script: omni-home-unresolved
    ("write_runtime_config", False),  # runtime_config_script: "never fatal"
    ("ship_plugin_trees", True),  # plugin_ensure_script: the plugin trees on the host
    ("prepare_run_dirs", True),  # scratch_script: scratch dir
    ("stage_ledger_snapshot", True),  # put_snapshot: ledger-snapshot-failed
    ("stage_file", True),  # put_file of the brief: upload failed
    ("stage_file", True),  # the CLAIM row the mirror reads
    ("mirror_ledger", True),  # ledger_mirror_script: claim-mirror-failed
    ("stop_lane_guard", False),  # lane_guard.reap_script
    ("drain_guard_spool", False),  # lane_guard.drain_script
    ("relay_reports", False),  # NodeLaneReportRelayEffect
    ("read_ledger_outbox", False),  # ledger_outbox_script
    ("read_lane_changes", False),  # changes_script
    ("closeout_worktrees", False),  # closeout_script
    ("hold_run_dir_guard", False),  # hold_run_dir_guard
    ("retain_run_artifacts", False),  # retention_script_text
)
FATAL_STEPS = ["resolve_registry_root", "ship_plugin_trees", "prepare_run_dirs"]
FATAL_STEPS += ["stage_ledger_snapshot", "stage_file", "mirror_ledger"]
OTHER_STEPS = [
    "write_runtime_config",
    "stop_lane_guard",
    "drain_guard_spool",
    "relay_reports",
    "read_ledger_outbox",
    "read_lane_changes",
    "closeout_worktrees",
    "hold_run_dir_guard",
]
TREE = ModelLanePluginTree(
    name="omni", source_digest="d" * 12, archive_path="/tmp/omni.tgz"
)


def overlay_provisioning() -> dict[str, Any]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    section: dict[str, Any] = document["remote_lane_start"]["provisioning"]
    return copy.deepcopy(section)


def order_of(plan: ModelLaneProvisioningPlan) -> tuple[tuple[str, bool], ...]:
    return tuple((entry.step.value, entry.fatal) for entry in plan.steps)


def with_fatal(plan: dict[str, Any], step: str, fatal: bool) -> dict[str, Any]:
    plan = copy.deepcopy(plan)
    for entry in plan["steps"]:
        if entry["step"] == step:
            entry["fatal"] = fatal
    return plan


def run(
    tmp_path: Path,
    script: ModelScriptedTransportScript | None = None,
    plan: dict[str, Any] | None = None,
    **overrides: Any,
) -> tuple[Rig, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rig = Rig(
        tmp_path,
        script
        or ModelScriptedTransportScript(
            exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
        ),
        cfg=settings(provisioning=plan or overlay_provisioning()),
    )
    result = rig.orchestrator.run(
        request(ledger_path=str(rig.ledger_path), plugin_trees=(TREE,), **overrides)
    )
    return rig, result


def host_ops(rig: Rig) -> list[str]:
    return [
        c.split()[0].removeprefix("host:") for c in rig.calls if c.startswith("host:")
    ]


# ------------------------------------------------------------------ the plan is the runner's order


def test_the_overlay_plan_is_the_runners_order_and_policy() -> None:
    plan = ModelLaneProvisioningPlan.model_validate(overlay_provisioning())
    assert order_of(plan) == RUNNER_ORDER


def test_failing_control_a_reordered_or_flipped_plan_is_not_the_runners_order() -> None:
    swapped = overlay_provisioning()
    swapped["steps"][3], swapped["steps"][4] = swapped["steps"][4], swapped["steps"][3]
    assert order_of(ModelLaneProvisioningPlan.model_validate(swapped)) != RUNNER_ORDER
    flipped = with_fatal(overlay_provisioning(), "write_runtime_config", True)
    assert order_of(ModelLaneProvisioningPlan.model_validate(flipped)) != RUNNER_ORDER


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda p: p["steps"].pop(4), "mirror_ledger needs"),
        (lambda p: p["steps"].insert(2, dict(p["steps"][1])), "listed twice"),
        (
            lambda p: p["steps"].insert(
                0,
                {
                    "step": "stop_lane_guard",
                    "phase": "after_close",
                    "fatal": True,
                    "timeout_seconds": 5,
                },
            ),
            "only before_engine steps are fatal",
        ),
        (lambda p: p.pop("runtime_config_target"), "both set or both empty"),
        (
            lambda p: p["steps"].insert(
                0,
                {
                    "step": "prepare_worktree",
                    "phase": "before_engine",
                    "fatal": True,
                    "timeout_seconds": 5,
                },
            ),
            "not a provisioning step",
        ),
    ],
)
def test_a_plan_the_transport_cannot_carry_out_is_refused_at_load(
    edit: Any, match: str
) -> None:
    plan = overlay_provisioning()
    edit(plan)
    with pytest.raises(ValidationError, match=match):
        ModelLaneProvisioningPlan.model_validate(plan)


# ------------------------------------------------------------------ golden chain


def test_golden_chain_every_step_runs_in_the_runners_order_around_the_claim_and_the_closing_row(
    tmp_path: Path,
) -> None:
    rig, result = run(
        tmp_path,
        ModelScriptedTransportScript(
            exit_polls=1,
            log_lines=("starting", stream_result(DONE_MESSAGE)),
            closeout_cells=("worktree=removed:/runs/alpha",),
        ),
        repo="omnimarket",
        sha="a" * 40,
    )

    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.exit_code == 0
    before = [
        "resolve_registry_root",
        "write_runtime_config",
        "ship_plugin_trees",
        "prepare_run_dirs",
    ]
    before += ["stage_ledger_snapshot", "stage_file", "stage_file", "mirror_ledger"]
    engine = [
        "prepare_worktree",
        "start_engine_detached",
        "read_log_tail",
        "read_engine_exit",
    ]
    after = [
        "stop_lane_guard",
        "drain_guard_spool",
        "relay_reports",
        "read_ledger_outbox",
    ]
    after += ["read_lane_changes", "closeout_worktrees"]
    ops = host_ops(rig)
    assert ops[: len(before)] == before
    assert ops[len(before) : len(before) + len(engine)] == engine
    assert ops[-len(after) - 2 :] == [
        *after,
        "hold_run_dir_guard",
        "retain_run_artifacts",
    ]
    # The CLAIM precedes every host call; the closing row follows closeout and precedes the hold and the retention.
    claim_at = rig.calls.index("ledger:append CLAIM")
    assert not any(c.startswith("host:") for c in rig.calls[:claim_at])
    terminal_at = rig.calls.index("ledger:append TERMINAL")
    assert (
        rig.calls.index(
            "host:closeout_worktrees host=h106 run=rlane-floor-alpha-a1-000001"
        )
        < terminal_at
    )
    assert terminal_at < rig.calls.index(
        "host:hold_run_dir_guard host=h106 run=rlane-floor-alpha-a1-000001"
    )
    # The receipt holds each step's result, all ok, retain included (the scripted transport runs it).
    records = result.receipt.provisioning
    assert [(r.step, r.fatal) for r in records] == list(RUNNER_ORDER)
    assert {r.status for r in records} == {"ok"}
    # The closeout step's cell is the TERMINAL's worktree cell.
    [terminal] = rig.ledger_rows("TERMINAL")
    assert cells(terminal)["worktree"] == "removed:/runs/alpha"
    assert (
        sum(1 for line in result.lines if line.startswith("REMOTE-LANE-PROVISION "))
        == 16
    )


def test_a_lane_with_no_plugin_trees_skips_that_step_as_the_runner_does(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(
            exit_polls=1, log_lines=(stream_result(DONE_MESSAGE),)
        ),
        cfg=settings(provisioning=overlay_provisioning()),
    )
    result = rig.orchestrator.run(request(ledger_path=str(rig.ledger_path)))

    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert "ship_plugin_trees" not in host_ops(rig)
    [ship] = [r for r in result.receipt.provisioning if r.step == "ship_plugin_trees"]
    assert ship.status == "skipped"


# ------------------------------------------------------------------ error chain


@pytest.mark.parametrize("step", FATAL_STEPS)
def test_error_chain_a_fatal_step_that_fails_ends_the_start_with_a_closing_row_and_no_engine(
    tmp_path: Path, step: str
) -> None:
    rig, result = run(
        tmp_path,
        ModelScriptedTransportScript(step_errors={step: "host refused the step"}),
    )

    assert result.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    assert result.exit_code == 1
    ops = host_ops(rig)
    assert "start_engine_detached" not in ops
    assert "prepare_worktree" not in ops
    [failed] = [r for r in result.receipt.provisioning if r.status == "failed"]
    assert failed.step == step
    assert failed.fatal is True
    assert failed.error == "host refused the step"
    # No step after the failed one ran in its phase; the closing steps ran, and one closing row stands.
    runner_steps = [name for name, _ in RUNNER_ORDER[:8]]
    first_failed_at = runner_steps.index(
        step
    )  # stage_file is staged twice: the first call fails
    assert ops[: first_failed_at + 1][-1] == step
    assert "mirror_ledger" not in ops or step == "mirror_ledger"
    assert "closeout_worktrees" in ops
    assert (
        "read_ledger_outbox" not in ops
    )  # the runner reads it for a lane whose engine started only
    assert "hold_run_dir_guard" not in ops
    [terminal] = rig.ledger_rows("TERMINAL")
    assert f"provisioning failed at {step}" in terminal
    assert result.receipt.final is True
    assert any(
        line.startswith("REMOTE-LANE-PROVISION-FAILED") and f"step={step}" in line
        for line in result.lines
    )


@pytest.mark.parametrize("step", OTHER_STEPS)
def test_error_chain_a_step_that_is_not_fatal_is_recorded_and_the_lane_goes_on(
    tmp_path: Path, step: str
) -> None:
    rig, result = run(
        tmp_path,
        ModelScriptedTransportScript(
            exit_polls=1,
            log_lines=(stream_result(DONE_MESSAGE),),
            step_errors={step: "step refused"},
        ),
    )

    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.exit_code == 0
    assert "start_engine_detached" in host_ops(rig)
    [failed] = [r for r in result.receipt.provisioning if r.status == "failed"]
    assert (failed.step, failed.error) == (step, "step refused")
    assert failed.fatal is False


def test_failing_control_the_overlays_fatal_flag_decides_the_behaviour(
    tmp_path: Path,
) -> None:
    """Flip one value in the overlay and the same host failure changes the outcome (red without the policy in the overlay)."""
    script = ModelScriptedTransportScript(
        exit_polls=1,
        log_lines=(stream_result(DONE_MESSAGE),),
        step_errors={"write_runtime_config": "no config on the launching host"},
    )
    _, as_shipped = run(tmp_path / "a", script)
    _, flipped = run(
        tmp_path / "b",
        script,
        with_fatal(overlay_provisioning(), "write_runtime_config", True),
    )
    _, relaxed = run(
        tmp_path / "c",
        ModelScriptedTransportScript(step_errors={"resolve_registry_root": "no root"}),
        with_fatal(overlay_provisioning(), "resolve_registry_root", False),
    )

    assert as_shipped.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert flipped.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    assert relaxed.outcome is not EnumRemoteLaneStartOutcome.START_FAILED or any(
        r.step == "resolve_registry_root" and r.status == "failed" and not r.fatal
        for r in relaxed.receipt.provisioning
    )


def test_a_step_the_transport_declares_not_run_is_not_run_and_a_fatal_one_ends_the_start(
    tmp_path: Path,
) -> None:
    retain_not_run = STEP_NOT_RUN_BY_NODE.format(op="retain_run_artifacts")
    _, lane = run(
        tmp_path / "a",
        ModelScriptedTransportScript(
            exit_polls=1,
            log_lines=(stream_result(DONE_MESSAGE),),
            step_errors={"retain_run_artifacts": retain_not_run},
        ),
    )
    config_not_run = STEP_NOT_RUN_BY_NODE.format(op="write_runtime_config")
    _, fatal = run(
        tmp_path / "b",
        ModelScriptedTransportScript(
            step_errors={"write_runtime_config": config_not_run}
        ),
        with_fatal(overlay_provisioning(), "write_runtime_config", True),
    )

    assert lane.outcome is EnumRemoteLaneStartOutcome.ENDED
    [retain] = [
        r for r in lane.receipt.provisioning if r.step == "retain_run_artifacts"
    ]
    assert retain.status == "not-run"
    assert fatal.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    [config] = [
        r for r in fatal.receipt.provisioning if r.step == "write_runtime_config"
    ]
    assert (config.status, config.fatal) == ("not-run", True)


def test_a_ledger_that_cannot_be_snapshotted_fails_that_fatal_step_without_a_host_call(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(),
        cfg=settings(provisioning=overlay_provisioning()),
    )
    result = rig.orchestrator.run(
        request(ledger_path=str(tmp_path / "not a ledger.txt"))
    )

    assert result.outcome is EnumRemoteLaneStartOutcome.START_FAILED
    assert "stage_ledger_snapshot" not in host_ops(rig)
    failed = [r for r in result.receipt.provisioning if r.status == "failed"]
    assert [r.step for r in failed] == ["stage_ledger_snapshot"]
    assert "not a plain" in (failed[0].error or "")


# ------------------------------------------------------------------ the real wiring reads the plan from the overlay


def test_build_real_hands_the_overlays_runtime_config_to_the_transport(
    tmp_path: Path,
) -> None:
    handler: Any = build_real(
        settings(provisioning=overlay_provisioning()),
        InMemoryLaneHostPool(snapshot()),
        stop_requested=lambda: False,
        sleep=lambda _s: None,
        echo=lambda _line: None,
    )
    # Private attributes read by name through vars(), as the real-wiring tests read them.
    leader: Any = vars(handler)["_build"](
        request(ledger_path=str(tmp_path / "ledger.md"))
    )
    transport: Any = vars(leader)["_login"]
    plan = overlay_provisioning()
    assert vars(transport)["_runtime_config"] == plan["runtime_config_text"]
    assert vars(transport)["_runtime_config_target"] == plan["runtime_config_target"]
