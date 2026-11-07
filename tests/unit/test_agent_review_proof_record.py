# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record-shape test of the agent review fallback proof record.

The record (``docs/audits/agent-review-fallback-proof.yaml``) is one YAML mapping.
This test reads it and fails when:

* any of the six observations is missing, or lacks its UTC time, its head sha, its
  mode, the units it ran with their shas, or the command that produced it;
* an observation among (4) to (6) carries more than the one defect it tests, lacks the
  expected typed reason, or its gate is not red afterwards;
* an observation is ``not-observed``, claims ``live`` without a check-run id, or is
  ``lab-integration`` without ``live_rerun_pending: true``;
* either repository lacks a live required-rules section with a read time.

The detector is exercised over the real record with exactly one defect applied at a
time, so a regression in the detector cannot pass unnoticed. ``AGENT_REVIEW_PROOF_RECORD``
points the main test at another file.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD_PATH = REPO_ROOT / "docs" / "audits" / "agent-review-fallback-proof.yaml"

_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

STATUS_PHASE_ONE = "phase-1-lab-integration"
STATUS_FINAL = "final"
MODES = ("live", "lab-integration", "not-observed")
REPOSITORIES = ("omnimarket", "omniclaude")
CASES = (1, 2, 3, 4, 5, 6)
SINGLE_DEFECT_CASES = (4, 5, 6)
# The verifier's typed refusal reasons a case may expect.
EXPECTED_REASONS: dict[int, tuple[str, ...]] = {
    4: ("forged_signature", "missing_signature"),
    5: ("blocked_verdict",),
    6: ("head_differs",),
}


def load_record(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"{path.name} is not a mapping")
    return document


def _is_time(value: Any) -> bool:
    return isinstance(value, str) and _UTC.match(value) is not None


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA.match(value) is not None


def _red(value: Any) -> bool:
    return value == "red"


def _units_problems(label: str, units: Any) -> list[str]:
    if not isinstance(units, list) or not units:
        return [f"{label}: names no unit it ran"]
    out: list[str] = []
    for unit in units:
        if (
            not isinstance(unit, dict)
            or not unit.get("name")
            or not _is_sha(unit.get("sha"))
        ):
            out.append(f"{label}: a unit lacks its name or its 40-hex sha")
    return out


def _mode_problems(label: str, obs: dict[str, Any], status: Any) -> list[str]:
    mode = obs.get("mode")
    if mode not in MODES:
        return [f"{label}: mode {mode!r} is none of {MODES}"]
    if mode == "not-observed":
        return [f"{label}: is not-observed ({obs.get('reason') or 'no typed reason'})"]
    if mode == "live" and not obs.get("check_run_id"):
        return [f"{label}: claims live without a check-run id"]
    if mode == "lab-integration":
        if status == STATUS_FINAL:
            return [f"{label}: a final record has no lab-integration observation"]
        if obs.get("live_rerun_pending") is not True:
            return [f"{label}: is lab-integration without live_rerun_pending: true"]
    if status == STATUS_FINAL and obs.get("live_rerun_pending"):
        return [f"{label}: a final record has no pending live re-run"]
    return []


def _case_problems(case: int, label: str, obs: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if case == 1:
        ids = obs.get("receipt_ids_after_second_dispatch")
        if not isinstance(ids, list) or len(ids) != 1:
            out.append(
                f"{label}: a second dispatch of the same head must leave exactly one receipt id"
            )
        if obs.get("receipt_reviews_on_pull_request") != 1:
            out.append(
                f"{label}: exactly one receipt review must appear on the pull request"
            )
    if case == 2:
        if not obs.get("reran_by"):
            out.append(f"{label}: does not name who reran")
        if obs.get("source") != "agent":
            out.append(f"{label}: the accepted decision does not carry source=agent")
        names = obs.get("required_names_after")
        if (
            not isinstance(names, dict)
            or not names
            or not all(v == "green" for v in names.values())
        ):
            out.append(f"{label}: the required names are not all green after the rerun")
    if case == 3:
        if obs.get("old_receipt_void_on_new_head") is not True:
            out.append(f"{label}: does not show the old receipt void on the new head")
        if not _red(obs.get("new_head_gate_before_new_review")):
            out.append(f"{label}: the new head is not red before a new review")
        if obs.get("new_head_gate_after_new_review") != "green":
            out.append(f"{label}: the new head is not green after a new review")
        if not _is_sha(obs.get("new_head_sha")) or obs.get("new_head_sha") == obs.get(
            "head_sha"
        ):
            out.append(f"{label}: names no distinct new head sha")
    if case in SINGLE_DEFECT_CASES:
        defects = obs.get("defects")
        if not isinstance(defects, list) or len(defects) != 1:
            out.append(
                f"{label}: carries {len(defects) if isinstance(defects, list) else 'no'} defects, not exactly one"
            )
        expected = obs.get("expected_reason")
        if not expected:
            out.append(f"{label}: lacks the expected typed reason")
        elif expected not in EXPECTED_REASONS[case]:
            out.append(
                f"{label}: expected reason {expected!r} is not one of {EXPECTED_REASONS[case]}"
            )
        elif obs.get("observed_reason") != expected:
            out.append(
                f"{label}: observed reason {obs.get('observed_reason')!r} is not the expected {expected!r}"
            )
        names = obs.get("required_names_after")
        if not _red(obs.get("gate_after")):
            out.append(f"{label}: the gate is not red afterwards")
        if (
            not isinstance(names, dict)
            or not names
            or not all(_red(v) for v in names.values())
        ):
            out.append(f"{label}: a required name is not red afterwards")
    return out


def validate(record: dict[str, Any]) -> list[str]:
    """Every way the record fails the ticket's falsifier, one message each."""
    problems: list[str] = []
    status = record.get("status")
    if status not in (STATUS_PHASE_ONE, STATUS_FINAL):
        problems.append(
            f"status {status!r} is neither {STATUS_PHASE_ONE!r} nor {STATUS_FINAL!r}"
        )
    if status == STATUS_FINAL:
        for unit in record.get("units") or []:
            if not isinstance(unit, dict) or not unit.get("tag"):
                problems.append("status is final but a unit names no tag the lab runs")
    observations = record.get("observations")
    by_case: dict[int, dict[str, Any]] = {}
    for obs in observations if isinstance(observations, list) else []:
        if isinstance(obs, dict) and isinstance(obs.get("case"), int):
            by_case[obs["case"]] = obs
    for case in CASES:
        label = f"observation {case}"
        obs = by_case.get(case)
        if obs is None:
            problems.append(f"{label}: missing")
            continue
        if not _is_time(obs.get("observed_at")):
            problems.append(f"{label}: lacks its UTC timestamp")
        if not _is_sha(obs.get("head_sha")):
            problems.append(f"{label}: lacks the 40-hex head sha it was observed on")
        if not obs.get("command"):
            problems.append(f"{label}: names no command that produced it")
        problems += _units_problems(label, obs.get("units"))
        problems += _mode_problems(label, obs, status)
        problems += _case_problems(case, label, obs)
    rules = record.get("required_rules")
    for repo in REPOSITORIES:
        section = rules.get(repo) if isinstance(rules, dict) else None
        if not isinstance(section, dict):
            problems.append(f"required_rules: no live section for {repo}")
            continue
        if not _is_time(section.get("read_at")):
            problems.append(f"required_rules: {repo} has no read time")
        if not section.get("read_via"):
            problems.append(f"required_rules: {repo} names no source-control read")
        contexts = section.get("contexts")
        if not isinstance(contexts, list) or not contexts:
            problems.append(f"required_rules: {repo} lists no required context")
    return problems


def _real() -> dict[str, Any]:
    return load_record(
        Path(os.environ.get("AGENT_REVIEW_PROOF_RECORD", str(RECORD_PATH)))
    )


def _obs(record: dict[str, Any], case: int) -> dict[str, Any]:
    found = [o for o in record["observations"] if o["case"] == case]
    assert len(found) == 1
    return found[0]


# -- the real record --------------------------------------------------------


def test_the_record_passes_the_shape_check() -> None:
    problems = validate(_real())
    assert not problems, "the proof record fails: " + "; ".join(problems)


def test_the_record_status_is_one_the_test_accepts() -> None:
    record = _real()
    assert record["status"] in (STATUS_PHASE_ONE, STATUS_FINAL)
    if record["status"] == STATUS_FINAL:
        assert all(
            o["mode"] == "live" and o["check_run_id"] for o in record["observations"]
        )
        assert not any(o.get("live_rerun_pending") for o in record["observations"])


def test_the_record_carries_no_private_surface() -> None:
    text = (
        Path(os.environ.get("AGENT_REVIEW_PROOF_RECORD", str(RECORD_PATH)))
    ).read_text("utf-8")
    assert not re.search(r"\bOMN-\d+\b", text, re.IGNORECASE), (
        "the public record names a ticket id"
    )
    assert not re.search(r"/(?:Users|Volumes|home)/", text), (
        "the public record names a local path"
    )
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), (
        "the public record names an address"
    )


# -- the detector, one defect at a time ------------------------------------


def _mutations() -> dict[str, Any]:
    def drop_case(r: dict[str, Any], case: int) -> None:
        r["observations"] = [o for o in r["observations"] if o["case"] != case]

    def set_obs(case: int, key: str, value: Any) -> Any:
        def apply(r: dict[str, Any]) -> None:
            _obs(r, case)[key] = value

        return apply

    def del_obs(case: int, key: str) -> Any:
        def apply(r: dict[str, Any]) -> None:
            del _obs(r, case)[key]

        return apply

    def two_defects(r: dict[str, Any]) -> None:
        _obs(r, 4)["defects"] = ["field changed after signing", "signature removed"]

    def drop_rules(r: dict[str, Any]) -> None:
        del r["required_rules"]["omniclaude"]

    def drop_read_time(r: dict[str, Any]) -> None:
        del r["required_rules"]["omnimarket"]["read_at"]

    return {
        "missing observation 1": (lambda r: drop_case(r, 1), "observation 1: missing"),
        "missing observation 3": (lambda r: drop_case(r, 3), "observation 3: missing"),
        "missing observation 6": (lambda r: drop_case(r, 6), "observation 6: missing"),
        "two defects": (two_defects, "not exactly one"),
        "no expected reason": (
            del_obs(5, "expected_reason"),
            "lacks the expected typed reason",
        ),
        "wrong observed reason": (
            set_obs(6, "observed_reason", "forged_signature"),
            "is not the expected",
        ),
        "gate not red": (
            set_obs(4, "gate_after", "green"),
            "the gate is not red afterwards",
        ),
        "name not red": (
            set_obs(5, "required_names_after", {"Hostile Review Gate": "green"}),
            "a required name is not red",
        ),
        "no timestamp": (del_obs(2, "observed_at"), "lacks its UTC timestamp"),
        "no head sha": (del_obs(3, "head_sha"), "lacks the 40-hex head sha"),
        "no units": (set_obs(1, "units", []), "names no unit it ran"),
        "no command": (del_obs(6, "command"), "names no command"),
        "not observed": (set_obs(2, "mode", "not-observed"), "is not-observed"),
        "live without check run": (
            set_obs(1, "mode", "live"),
            "live without a check-run id",
        ),
        "lab integration not flagged pending": (
            set_obs(1, "live_rerun_pending", False),
            "without live_rerun_pending",
        ),
        "no rerun owner": (del_obs(2, "reran_by"), "does not name who reran"),
        "not source agent": (set_obs(2, "source", "ci"), "source=agent"),
        "new head not red": (
            set_obs(3, "new_head_gate_before_new_review", "green"),
            "not red before a new review",
        ),
        "second dispatch posts a second receipt": (
            set_obs(1, "receipt_ids_after_second_dispatch", ["a", "b"]),
            "exactly one receipt id",
        ),
        "required rules missing for one repository": (
            drop_rules,
            "no live section for omniclaude",
        ),
        "required rules without read time": (
            drop_read_time,
            "omnimarket has no read time",
        ),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_the_check_fails_with_exactly_one_defect_applied(name: str) -> None:
    mutate, expected = _mutations()[name]
    record = copy.deepcopy(load_record(RECORD_PATH))
    assert validate(record) == []
    mutate(record)
    problems = validate(record)
    assert any(expected in p for p in problems), (
        f"{name}: {expected!r} not in {problems}"
    )


def test_a_final_record_refuses_a_pending_live_rerun() -> None:
    record = copy.deepcopy(load_record(RECORD_PATH))
    record["status"] = STATUS_FINAL
    problems = validate(record)
    assert any("final record has no lab-integration" in p for p in problems)


def test_a_record_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "record.yaml"
    empty.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="not a mapping"):
        load_record(empty)
