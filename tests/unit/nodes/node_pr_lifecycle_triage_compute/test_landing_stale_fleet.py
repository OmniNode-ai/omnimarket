# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale-fleet red class: a red whose failing check's job log names runner label sets carried ONLY by
offline runners, once fleet-status reads those labels carried by an online runner.

Verdicts are asked through ``HandlerClassifyLandingRed().handle(ModelLandingRedFacts(...))``; the two recorded
job logs are the runner-label-exists checks of omnibase_infra 5908 and 5909 on 2026-11-12, each saying the label
set ``self-hosted, omnibase-ci, arch-amd64`` is carried ONLY by offline runners.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_landing_red import (
    HandlerClassifyLandingRed,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_stale_fleet import (
    offline_only_label_sets,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_red_facts import (
    ModelLandingRedFacts,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "landing_red_rules"

# The two recorded job logs: the runner-label-exists check of omnibase_infra 5908 and 5909 on 2026-11-12.
FIXTURE_LOGS = {
    "5908": (
        FIXTURES / "omnibase_infra_5908_runner_label_exists_job_131219942690.log"
    ).read_text(),
    "5909": (
        FIXTURES / "omnibase_infra_5909_runner_label_exists_job_131206795894.log"
    ).read_text(),
}

# Synthetic fleet-status reading of one online runner: a runner that carries the whole label set the gate
# judged offline-only. (Synthetic: the recorded logs name no online runner, so we supply one to prove the
# stale-fleet branch fires.)
ONLINE = (("self-hosted", "omnibase-ci", "arch-amd64", "online-runner-1"),)

CHECK = "runner-label-exists / runner-label-exists"


def _facts(
    red: Sequence[str],
    *,
    failure_logs: dict[str, str | None] | None = None,
    online_runner_labels: tuple[tuple[str, ...], ...] | None = None,
    runs: Sequence[Sequence[str | None]] = (
        ("CI Summary", "completed", "failure", ""),
        (CHECK, "completed", "failure", ""),
    ),
    companion_merged: bool = False,
) -> ModelLandingRedFacts:
    return ModelLandingRedFacts(
        red=tuple(red),
        runs=tuple(tuple(r) for r in runs),
        companion_merged=companion_merged,
        failure_logs=failure_logs,
        online_runner_labels=online_runner_labels,
    )


def _class(facts: ModelLandingRedFacts) -> str:
    return str(HandlerClassifyLandingRed().handle(facts).red_class)


@pytest.mark.parametrize("job", ["5908", "5909"])
@pytest.mark.parametrize("companion_merged", [False, True])
def test_stale_fleet_verdict(job: str, companion_merged: bool) -> None:
    facts = _facts(
        ["CI Summary", CHECK],
        failure_logs={CHECK: FIXTURE_LOGS[job]},
        online_runner_labels=ONLINE,
        companion_merged=companion_merged,
    )
    verdict = HandlerClassifyLandingRed().handle(facts)
    assert verdict.red_class == "stale_fleet"
    assert list(verdict.worker_reds) == ["CI Summary", CHECK]


@pytest.mark.parametrize("job", ["5908", "5909"])
def test_offline_only_label_sets(job: str) -> None:
    assert offline_only_label_sets(FIXTURE_LOGS[job]) == (
        frozenset({"self-hosted", "omnibase-ci", "arch-amd64"}),
    )


def test_fleet_status_unread_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={CHECK: FIXTURE_LOGS["5908"]},
                online_runner_labels=None,
            )
        )
        == "product"
    )


def test_fleet_still_offline_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={CHECK: FIXTURE_LOGS["5908"]},
                online_runner_labels=(),
            )
        )
        == "product"
    )


def test_online_runner_lacks_the_whole_set_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={CHECK: FIXTURE_LOGS["5908"]},
                online_runner_labels=(("self-hosted", "omnibase-ci"),),
            )
        )
        == "product"
    )


def test_no_failure_logs_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs=None,
                online_runner_labels=ONLINE,
            )
        )
        == "product"
    )


def test_log_without_offline_only_text_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={CHECK: "FAILED tests/test_x.py::test_y - assert 1 == 2"},
                online_runner_labels=ONLINE,
            )
        )
        == "product"
    )


def test_failure_logs_missing_the_check_is_product() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={"CI Summary": FIXTURE_LOGS["5908"]},
                online_runner_labels=ONLINE,
            )
        )
        == "product"
    )


def test_real_product_failure_beside_stale_fleet_is_product() -> None:
    assert (
        _class(
            _facts(
                ["Tests (Split 1/4)", CHECK],
                failure_logs={
                    CHECK: FIXTURE_LOGS["5908"],
                    "Tests (Split 1/4)": "FAILED tests/test_x.py::test_y - assert 1 == 2",
                },
                online_runner_labels=ONLINE,
            )
        )
        == "product"
    )


def test_all_timed_out_keeps_runner_saturation() -> None:
    assert (
        _class(
            _facts(
                ["CI Summary", CHECK],
                failure_logs={CHECK: FIXTURE_LOGS["5908"]},
                online_runner_labels=ONLINE,
                runs=(
                    ["CI Summary", "completed", "timed_out", ""],
                    [CHECK, "completed", "timed_out", ""],
                ),
            )
        )
        == "runner_saturation"
    )


def test_cascade_head_keeps_cascade() -> None:
    # The change-control cascade rule comes first in the order: stale-fleet facts beside it change nothing.
    red = ["OCC Companion Merged Gate (OMN-15214)", "CI Summary"]
    runs = [(n, "completed", "failure", "") for n in red]
    assert (
        _class(
            _facts(
                red,
                runs=runs,
                failure_logs={CHECK: FIXTURE_LOGS["5908"]},
                online_runner_labels=ONLINE,
                companion_merged=True,
            )
        )
        == "cascade"
    )


def test_facts_without_the_new_fields_still_validate() -> None:
    facts = ModelLandingRedFacts(
        red=("CI Summary", CHECK),
        runs=(
            ["CI Summary", "completed", "failure", ""],
            [CHECK, "completed", "failure", ""],
        ),
        companion_merged=False,
    )
    assert facts.failure_logs is None
    assert facts.online_runner_labels is None
    assert _class(facts) == "product"


def test_extra_unknown_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="not_a_field"):
        ModelLandingRedFacts(
            red=("CI Summary",),
            companion_merged=False,
            not_a_field=True,
        )
