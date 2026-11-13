# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the start event and branch install this node verifies, against their producers.

The start event's producer, the deploy agent (five whys R1), does not exist yet, and the
build manifest's ``ModelBranchInstall`` lives on node_dev_running_revision_compute, lane
l2-rollevidence-6245, branch omn-23993-l2-roll-evidence, not merged. Neither other side can
be imported here, and a cross-repository or cross-branch check that skips when the other
side is absent is refused by the skip ratchet. So both wire shapes are spelled below. This
test goes red when OUR models change shape; when the other side changes, the spelled
contract is updated in the same change that follows it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.dev_roll import (
    EnumDevRollHealth,
    ModelDevRollRecord,
)
from omnimarket.nodes.node_dev_roll_verify_compute.handlers import (
    decide_dev_roll_verify,
)
from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerdict,
    EnumDevStartCause,
    ModelDevBranchInstall,
    ModelDevRollVerified,
    ModelDevRollVerifyPolicy,
    ModelDevRuntimeStarted,
)
from tests.unit.nodes.node_dev_roll_verify_compute.conftest import (
    ROLL_FINISH,
    ROLL_START,
    facts,
    start,
)

pytestmark = pytest.mark.unit

AGENT_START_EVENT_CONTRACT: dict[str, Any] = {
    "topic": "onex.evt.omnimarket.dev-runtime-started.v1",
    "required": {"event_id", "job_id", "runtime_lane", "started_at", "cause"},
    "causes": {"roll_recreate", "branch_install_reapply", "other"},
}
# The three wire fields of ModelBranchInstall (node_dev_running_revision_compute),
# read on lane l2-rollevidence-6245, branch omn-23993-l2-roll-evidence (unmerged).
BUILD_MANIFEST_BRANCH_INSTALL = {"repository", "branch", "sha"}

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = ROOT / "src/omnimarket/nodes/node_dev_roll_verify_compute/contract.yaml"
_DISPATCH = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["runtime_dispatch"]


def test_our_start_event_has_exactly_the_agents_required_fields() -> None:
    assert (
        set(ModelDevRuntimeStarted.model_fields)
        == AGENT_START_EVENT_CONTRACT["required"]
    ), (
        "ModelDevRuntimeStarted (ours) or the deploy agent's dev_runtime_started "
        "event (theirs, five whys R1) changed shape"
    )


def test_our_start_causes_are_the_agents_declared_causes() -> None:
    values = {cause.value for cause in EnumDevStartCause}
    assert values == AGENT_START_EVENT_CONTRACT["causes"], (
        "EnumDevStartCause (ours) or the deploy agent's declared start causes "
        "(theirs) changed shape"
    )


def test_the_agents_start_topic_is_the_one_our_contract_names() -> None:
    assert AGENT_START_EVENT_CONTRACT["topic"] in CONTRACT.read_text(
        encoding="utf-8"
    ), (
        "the deploy agent's dev_runtime_started topic (theirs) or our contract.yaml "
        "header (ours) renamed the topic"
    )


def test_our_install_has_exactly_the_build_manifests_fields() -> None:
    assert set(ModelDevBranchInstall.model_fields) == BUILD_MANIFEST_BRANCH_INSTALL, (
        "ModelDevBranchInstall (ours) or the build manifest's ModelBranchInstall of "
        "node_dev_running_revision_compute (theirs, omn-23993-l2-roll-evidence) "
        "changed shape"
    )


def test_our_contract_declares_the_three_verify_topics() -> None:
    assert (
        _DISPATCH["command_topic"] == "onex.cmd.omnimarket.dev-roll-verify-requested.v1"
    ), "contract.yaml (ours) renamed the command topic"
    assert (
        _DISPATCH["terminal_events"]["success"]
        == "onex.evt.omnimarket.dev-roll-verified.v1"
    ), "contract.yaml (ours) renamed the success terminal event"
    assert (
        _DISPATCH["terminal_events"]["failure"]
        == "onex.evt.omnimarket.dev-roll-verify-refused.v1"
    ), "contract.yaml (ours) renamed the failure terminal event"


def test_r4s_roll_record_takes_its_last_start_time_from_the_verifier(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    verified = decide_dev_roll_verify(
        facts((start(seconds_after_roll_start=90),)), policy
    )
    assert isinstance(verified, ModelDevRollVerified)
    assert verified.verdict is EnumDevRollVerdict.PASSED, (
        "a one-start roll must pass: the verifier (ours) or the verdict rules changed"
    )
    # R4 measures the minimum start interval from the last runtime start it observed
    # (five whys R4), so the record's runtime start is the verifier's last start.
    record = ModelDevRollRecord(
        job_id=verified.job_id,
        git_ref="a" * 40,
        runtime_started_at=verified.last_start_at,
        finished_at=ROLL_FINISH,
        health=EnumDevRollHealth.HEALTHY,
        healthy_since=ROLL_FINISH,
    )
    assert record.runtime_started_at == ROLL_START + timedelta(seconds=90), (
        "ModelDevRollRecord (node_dev_roll_request_compute, R4's side) or "
        "ModelDevRollVerified.last_start_at (the verifier's side) changed shape"
    )
