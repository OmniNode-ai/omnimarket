# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test for the seam between the lane's delegation step, the code-edit node and the delegate engine (OMN-23434).

A lane drafts code with ``delegation_step.py code``, which runs ``onex code-edit run`` and reads the one JSON line the node prints
(``ModelCodeEditResult``); each model turn of the node goes through ``onex delegate``, whose terminal the CLI reads with
``resolve_delegate_terminal``. The step is plugin code and cannot be imported here, so this test pins what it sends and reads as
literals written below, and checks both against the node's real models, CLI and the terminal resolver. A key renamed or dropped on
either side fails here. The terminal fixture is a delegate terminal the dev lane's runtime served for a code draft (host, tenant
and text sanitized).

Profiles (``CODE_EDIT_SEAM_LANE``): the in-memory profile runs the node loop on fake ports and resolves the fixture terminal. The
redpanda profile sends a real code draft through ``onex delegate`` to the named bus lane and resolves the terminal the lane
answered with, so it needs a lane the host holds a login for; it is skipped when none is named. A client whose omnibase_core
predates a field the runtime now sends reads ``extra_forbidden`` there (h051 and h201, 2026-11-10: core 0.47.69 against a runtime
that sends ``ceiling_source``), which is how the live reading fails.

The failing controls plant a named rename and require the golden or the error chain to fail. ``CODE_EDIT_SEAM_MUTATE=<name>`` runs
the whole file under one mutation to read it fail.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Final, Literal
from unittest import mock

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationBudgetEvidence
from omnibase_infra.cli.delegate_terminal_resolver import (
    DelegateTerminalUnresolvedError,
    resolve_delegate_terminal,
)
from pydantic import BaseModel, ConfigDict

from omnimarket.cli.cli_code_edit import code_edit_group
from omnimarket.models.model_delegated_code_edit import ModelCodeEditResult
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    ModelTurnReply,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit

LANE_ENV: Final = "CODE_EDIT_SEAM_LANE"
ROOT_ENV: Final = "CODE_EDIT_SEAM_REGISTRY_ROOT"
MUTATE_ENV: Final = "CODE_EDIT_SEAM_MUTATE"
FIXTURE: Final = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation"
    / "delegate_terminal_envelope_code_generation.json"
)

# ---------------------------------------------------------------- the pinned wire
# Written out here, not read from the models: a model that moves must fail this test.
# What delegation_step.py reads from the node's one JSON result line.
STEP_READS_RESULT_KEYS: Final = frozenset(
    {
        "loop_run_id",
        "status",
        "turns",
        "delegate_run_ids",
        "changed_paths",
        "refusals",
        "rubric_outcome",
        "detail",
    }
)
# What delegation_step.py passes to `onex code-edit run`.
STEP_SENDS_FLAGS: Final = frozenset(
    {
        "--worktree",
        "--task-file",
        "--caller-lane",
        "--ticket",
        "--omnibase-path",
        "--state-root",
        "--max-turns",
        "--writable",
        "--check",
        "--context",
        "--delegate-in-process",
    }
)
# What the step needs of the delegate terminal's budget evidence, and what the resolver reads of the terminal.
BUDGET_EVIDENCE_READ_KEYS: Final = frozenset(
    {
        "requested_timeout_seconds",
        "task_class_timeout_ceiling_seconds",
        "execution_timeout_seconds",
        "terminal_delivery_margin_seconds",
    }
)
RUN_ID_LINE: Final = re.compile(r"dispatching: run ([0-9a-f-]{36}) ")


class ModelCodeEditSeamOverlay(BaseModel):
    """Every setting of one seam run; the in-memory profile is the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: Literal["inmemory", "redpanda"] = "inmemory"
    lane: str = ""
    registry_root: str = ""
    timeout_s: int = 300

    @classmethod
    def for_profile(cls, profile: str) -> ModelCodeEditSeamOverlay:
        if profile == "redpanda":
            return cls(
                profile="redpanda",
                lane=os.environ.get(LANE_ENV, ""),
                registry_root=os.environ.get(ROOT_ENV, ""),
            )
        return cls()


@pytest.fixture(
    params=[
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ]
)
def overlay(request: pytest.FixtureRequest) -> ModelCodeEditSeamOverlay:
    chosen = ModelCodeEditSeamOverlay.for_profile(request.param)
    if chosen.profile == "redpanda" and not (chosen.lane and chosen.registry_root):
        pytest.skip(f"no broker: {LANE_ENV} and {ROOT_ENV} name none")
    return chosen


# ---------------------------------------------------------------- the node side
def node_result(*, accepted: bool) -> dict[str, Any]:
    """The node's result line, as the loop produces it: accepted, or two dead delegate runs."""
    if accepted:
        ports = FakePorts(
            [_reply(1, _a("finish", summary="done"))], check_passes=[True]
        )
    else:
        dead = ModelTurnReply(
            run_id="", ok=False, invalid_reason="onex delegate exited 1"
        )
        ports = FakePorts([dead, dead])
    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request())
    return result.model_dump(mode="json")


def step_view(line: dict[str, Any]) -> dict[str, Any]:
    """What the step reads of the line, by the keys it reads them at."""
    return {key: line[key] for key in STEP_READS_RESULT_KEYS}


def step_counts_ok(view: dict[str, Any]) -> bool:
    """The step's own rule: accepted, and at least one delegate run id."""
    return view["status"] == "accepted" and bool(view["delegate_run_ids"])


def step_retries_in_process(view: dict[str, Any]) -> bool:
    """The step's own rule: a delegate_failed loop with no run id is retried once with --delegate-in-process."""
    return view["status"] == "delegate_failed" and not view["delegate_run_ids"]


def run_command_flags() -> set[str]:
    """Every option name the node's CLI accepts on ``onex code-edit run``."""
    return {
        opt for param in code_edit_group.commands["run"].params for opt in param.opts
    }


# ---------------------------------------------------------------- the engine side
def evidence_wire() -> dict[str, Any]:
    """The budget evidence the fixture's runtime sent, kept to the fields the installed core's model declares."""
    sent = json.loads(FIXTURE.read_text())["payload"]["budget_evidence"]
    declared = ModelDelegationBudgetEvidence.model_fields
    return ModelDelegationBudgetEvidence(
        **{k: v for k, v in sent.items() if k in declared}
    ).model_dump(mode="json")


def fixture_terminal(evidence: dict[str, Any]) -> dict[str, Any]:
    envelope = copy.deepcopy(json.loads(FIXTURE.read_text()))
    envelope["payload"]["budget_evidence"] = evidence
    return envelope


def live_receipt(overlay: ModelCodeEditSeamOverlay) -> dict[str, Any]:
    """One real code draft through ``onex delegate`` to the lane's runtime; the receipt the CLI wrote for it."""
    root = Path(overlay.registry_root)
    prompt = "Write a Python function add_one(value: int) -> int that returns value + 1. Reply with the code only."
    with tempfile.TemporaryDirectory() as state:
        argv = [
            "bash",
            str(root / "omnibase_infra" / "scripts" / "onex"),
            "delegate",
            prompt,
            "--task-type",
            "code_generation",
            "--ticket",
            "OMN-23434",
            "--caller-lane",
            "code-edit-seam",
            "--bus",
            "kafka",
            "--lane",
            overlay.lane,
            "--locus",
            "deployed-lane",
            "--state-root",
            state,
        ]
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=overlay.timeout_s, check=False
        )
        match = RUN_ID_LINE.search(done.stdout + done.stderr)
        assert match, (done.stdout + done.stderr)[-600:]
        loaded: dict[str, Any] = json.loads(
            (Path(state) / "runs" / match[1] / "receipt.json").read_text()
        )
    return loaded


# ---------------------------------------------------------------- failing controls
def _drop_key(data: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    renamed = dict(data)
    renamed[new] = renamed.pop(old)
    return renamed


@contextlib.contextmanager
def step_sends_renamed_flag() -> Iterator[None]:
    """The node's CLI calls ``--writable`` by another name than the step sends it."""
    param = next(
        p for p in code_edit_group.commands["run"].params if "--writable" in p.opts
    )
    renamed = ["--allow-write" if opt == "--writable" else opt for opt in param.opts]
    with mock.patch.object(param, "opts", renamed):
        yield


def _node_renames_delegate_run_ids() -> AbstractContextManager[None]:
    real = ModelCodeEditResult.model_dump

    def model_dump(self: ModelCodeEditResult, **options: Any) -> dict[str, Any]:
        return _drop_key(real(self, **options), "delegate_run_ids", "run_ids")

    return mock.patch.object(ModelCodeEditResult, "model_dump", model_dump)


def _core_adds_budget_field() -> AbstractContextManager[None]:
    """The runtime sends ``ceiling_origin`` where the client's core declares no such field."""
    real = ModelDelegationBudgetEvidence.model_dump

    def model_dump(
        self: ModelDelegationBudgetEvidence, **options: Any
    ) -> dict[str, Any]:
        return {**real(self, **options), "ceiling_origin": "packaged"}

    return mock.patch.object(ModelDelegationBudgetEvidence, "model_dump", model_dump)


MUTATIONS: Final[dict[str, Callable[[], AbstractContextManager[None]]]] = {
    "step-sends-renamed-flag": step_sends_renamed_flag,
    "node-renames-delegate-run-ids": _node_renames_delegate_run_ids,
    "core-adds-budget-field": _core_adds_budget_field,
}


@pytest.fixture(autouse=True)
def injected_break() -> Iterator[None]:
    """Run the whole file under one named mutation, to read it fail for real."""
    name = os.environ.get(MUTATE_ENV)
    if not name:
        yield
        return
    with MUTATIONS[name]():
        yield


# ---------------------------------------------------------------- the chains
def assert_node_golden() -> None:
    view = step_view(node_result(accepted=True))
    assert view["status"] == EnumCodeEditStatus.ACCEPTED.value == "accepted"
    assert step_counts_ok(view)
    assert run_command_flags() >= STEP_SENDS_FLAGS


def assert_node_error() -> None:
    view = step_view(node_result(accepted=False))
    assert (
        view["status"] == EnumCodeEditStatus.DELEGATE_FAILED.value == "delegate_failed"
    )
    assert not step_counts_ok(view)
    assert step_retries_in_process(view)
    assert view["detail"]


def assert_engine_golden(overlay: ModelCodeEditSeamOverlay) -> None:
    if overlay.profile == "redpanda":
        receipt = live_receipt(overlay)
        # A terminal the client cannot read is kept raw on the receipt: resolving it raises, which is the red reading.
        if "terminal_payload" in receipt:
            resolve_delegate_terminal(receipt["terminal_payload"])
        assert receipt["status"] == "success", receipt
        assert set(receipt["budget_evidence"]) >= BUDGET_EVIDENCE_READ_KEYS
        return
    resolved = resolve_delegate_terminal(fixture_terminal(evidence_wire()))
    assert resolved.attempts
    evidence = resolved.budget_evidence
    assert evidence is not None
    assert set(type(evidence).model_fields) >= BUDGET_EVIDENCE_READ_KEYS


def assert_engine_error() -> None:
    """A field the client's models do not declare makes the terminal unreadable, which is what the step saw as delegate_failed."""
    terminal = fixture_terminal({**evidence_wire(), "ceiling_origin": "packaged"})
    with pytest.raises(DelegateTerminalUnresolvedError) as refused:
        resolve_delegate_terminal(terminal)
    assert "extra_forbidden" in str(refused.value)
    assert "ceiling_origin" in str(refused.value)


def test_golden_the_step_reads_an_accepted_loop_and_the_engine_terminal_resolves(
    overlay: ModelCodeEditSeamOverlay,
) -> None:
    if overlay.profile == "inmemory":
        assert_node_golden()
    assert_engine_golden(overlay)


def test_error_a_dead_delegate_run_is_retried_and_an_unreadable_terminal_is_refused() -> (
    None
):
    assert_node_error()
    assert_engine_error()


def test_the_pinned_wire_is_what_the_models_declare() -> None:
    """A field added to or renamed in a model must be a decision made here, on both sides."""
    assert set(ModelCodeEditResult.model_fields) >= STEP_READS_RESULT_KEYS
    assert {m.value for m in EnumCodeEditStatus} >= {"accepted", "delegate_failed"}
    assert run_command_flags() >= STEP_SENDS_FLAGS
    assert set(ModelDelegationBudgetEvidence.model_fields) >= BUDGET_EVIDENCE_READ_KEYS


def run_every_chain(overlay: ModelCodeEditSeamOverlay) -> None:
    assert_node_golden()
    assert_node_error()
    assert_engine_golden(overlay)
    assert_engine_error()


def test_control_the_unmutated_chains_are_green() -> None:
    run_every_chain(ModelCodeEditSeamOverlay())


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_control_the_seam_is_red_under_a_planted_rename(mutation: str) -> None:
    with (
        MUTATIONS[mutation](),
        pytest.raises(
            (AssertionError, KeyError, ValueError, DelegateTerminalUnresolvedError)
        ),
    ):
        run_every_chain(ModelCodeEditSeamOverlay())
