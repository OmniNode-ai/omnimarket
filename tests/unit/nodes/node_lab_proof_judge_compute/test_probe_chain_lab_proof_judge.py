# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: the pool driver sends the lab probe's text and the node reads the whole changed-path rule (OMN-23715).

The chain runs through the CLI exactly as the driver calls it. The failing controls show a runtime node that no log
mentions still FAILS, a declared node with no substitute proof still FAILS, and a probe with no live-grep line is NO_CHECK,
never a pass.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_judge_verdict import (
    EnumJudgeVerdict,
)
from omnimarket.nodes.node_lab_proof_judge_compute.runtime_lab_proof_judge import main

HEAD = "6de58c0cd262843faac9996c53e549b7eba17a50"
REPO = "OmniNode-ai/omnimarket"
BARE = "omnimarket"
DECLARED = "node_overlay_drift_effect"
UNDECLARED = "node_pr_landing_decision_compute"


def _request(node: str, probe: str) -> dict[str, object]:
    return {
        "repo": REPO,
        "pr_number": 5811,
        "head": HEAD,
        "changed_paths": [f"src/omnimarket/nodes/{node}/handlers/handler_x.py"],
        "probe": probe,
    }


def _pool_run(node: str, probe: str, subjects: int = 1) -> dict[str, str]:
    """The pool driver's clone and probe text, as the driver hands them over."""
    clone = "".join(
        f"pr-subject {BARE}#{5811 + i} head={HEAD} base={'3' * 40} merge-base={'3' * 40} "
        f"diff-digest=sha256:{'a' * 64}\n"
        f"pr-changed {BARE}#{5811 + i} src/omnimarket/nodes/{node}/handlers/handler_x.py\n"
        for i in range(subjects)
    )
    return {"clone": clone, "probe": probe}


def _probe(node: str, count: int) -> str:
    return f"live-grep mention counts\n  runtime-main mentions [.]{node}[.]: {count}\n"


def _call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    stdin: str,
) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    head: str,
) -> None:
    rc, _, _ = _call(
        monkeypatch,
        capsys,
        [
            "record-substitute",
            "--repo",
            REPO,
            "--node",
            DECLARED,
            "--head",
            head,
            "--host",
            "labhost",
            "--host-class",
            "lab",
            "--command",
            f"{DECLARED} own tests",
            "--exit-code",
            "0",
            "--log-file",
            "/logs/own.log",
            "--finished-at",
            "2026-11-11T11:00:00+00:00",
        ],
        "",
    )
    assert rc == 0


def test_golden_probe_that_every_log_mentions_passes(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["changed-path-live"],
        json.dumps(_request(UNDECLARED, _probe(UNDECLARED, 3))),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "PASS"
    assert answer["ok"] is True
    assert answer["notes"] == []


def test_golden_declared_node_no_log_mentions_reads_not_applicable_with_proof(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, HEAD)
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["changed-path-live"],
        json.dumps(_request(DECLARED, _probe(DECLARED, 0))),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "NOT_APPLICABLE"
    assert answer["ok"] is True
    assert answer["class_id"] == "runs_outside_runtime"
    assert answer["substitute_proofs"][0]["node"] == DECLARED
    assert answer["notes"] == [
        f"changed path not live, no runtime log mentions: {DECLARED}"
    ]


def test_failing_control_runtime_node_no_log_mentions_still_fails(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, HEAD)
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["changed-path-live"],
        json.dumps(_request(UNDECLARED, _probe(UNDECLARED, 0))),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "FAIL"
    assert answer["ok"] is False
    assert answer["unmatched_nodes"] == [UNDECLARED]


def test_failing_control_declared_node_without_substitute_proof_fails(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, "b" * 40)
    _, out, _ = _call(
        monkeypatch,
        capsys,
        ["changed-path-live"],
        json.dumps(_request(DECLARED, _probe(DECLARED, 0))),
    )
    answer = json.loads(out)
    assert answer["verdict"] == "FAIL"
    assert answer["missing_proof_nodes"] == [DECLARED]


def test_golden_probe_with_no_live_grep_line_is_no_check_never_a_pass(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["changed-path-live"],
        json.dumps(_request(UNDECLARED, "stack up\n")),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "NO_CHECK"
    assert answer["ok"] is False


def test_error_chain_request_with_both_sources_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    both = {**_request(DECLARED, _probe(DECLARED, 0)), "base_changed_path_live": True}
    rc, out, err = _call(monkeypatch, capsys, ["changed-path-live"], json.dumps(both))
    assert rc == 3
    assert out == ""
    assert "INDETERMINATE" in err


def test_error_chain_request_with_neither_source_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    neither = _request(DECLARED, "")
    del neither["probe"]
    rc, out, err = _call(
        monkeypatch, capsys, ["changed-path-live"], json.dumps(neither)
    )
    assert rc == 3
    assert out == ""
    assert "INDETERMINATE" in err


def test_golden_pool_run_declared_node_reads_not_applicable_with_class_and_proof(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(monkeypatch, capsys, HEAD)
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["pool-run"],
        json.dumps(_pool_run(DECLARED, _probe(DECLARED, 0))),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "NOT_APPLICABLE"
    assert answer["evidence"] == (
        f"NOT-APPLICABLE class=runs_outside_runtime entries={DECLARED} "
        f"substitute={DECLARED}-{HEAD[:12]}-labhost; "
        f"changed path not live, no runtime log mentions: {DECLARED}"
    )


def test_failing_control_pool_run_runtime_node_no_log_mentions_still_fails(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _call(
        monkeypatch,
        capsys,
        ["pool-run"],
        json.dumps(_pool_run(UNDECLARED, _probe(UNDECLARED, 0))),
    )
    answer = json.loads(out)
    assert rc == 0
    assert answer["verdict"] == "FAIL"
    assert answer["unmatched_nodes"] == [UNDECLARED]


def test_pool_run_with_two_pull_requests_has_no_check(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _call(
        monkeypatch,
        capsys,
        ["pool-run"],
        json.dumps(_pool_run(DECLARED, _probe(DECLARED, 0), subjects=2)),
    )
    assert json.loads(out)["verdict"] == "NO_CHECK"


def test_pool_run_with_no_pull_request_has_no_check(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _call(
        monkeypatch,
        capsys,
        ["pool-run"],
        json.dumps({"clone": "", "probe": _probe(DECLARED, 0)}),
    )
    assert json.loads(out)["verdict"] == "NO_CHECK"


def test_error_chain_pool_run_malformed_request_is_indeterminate(
    deployed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = _call(monkeypatch, capsys, ["pool-run"], '{"clone": "x"}')
    assert rc == 3
    assert out == ""
    assert "INDETERMINATE" in err


def test_seam_with_the_pool_driver_request_and_answer_keys() -> None:
    """The pool driver (omnibase_infra prepr_runtime_pool.declared_changed_path) sends exactly these keys and reads these.

    Its own test (tests/unit/scripts/test_prepr_pool_declared_judge_omn23642.py) holds the other side of the pair; a
    change to either set fails one of the two.
    """
    from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
        ModelChangedPathLiveResult,
    )
    from omnimarket.nodes.node_lab_proof_judge_compute.models.model_pool_run import (
        ModelPoolRunRequest,
    )

    assert set(ModelPoolRunRequest.model_fields) == {"clone", "probe"}
    assert {"verdict", "ok", "evidence", "notes"} <= set(
        ModelChangedPathLiveResult.model_fields
    )
    assert {v.value for v in EnumJudgeVerdict} == {
        "PASS",
        "FAIL",
        "NOT_APPLICABLE",
        "NO_CHECK",
    }
