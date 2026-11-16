# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the delegation history effect: from the three readings to the written file.

The golden chain reads the projection, the routing overlay and the reports of two hosts and writes a document with all three
sections filled. Each error is a named injected break, and each turns the reading: a projection that never answers keeps the
previous file, a host that is down is named and the other host's experiments are still served, an unset ssh variable names itself,
and the same report on two hosts is one experiment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.floor_delegation_history import (
    ModelDelegationHistory,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.handler_floor_delegation_history import (
    HandlerFloorDelegationHistory,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelFloorDelegationHistoryRequest,
)

from .support import (
    ENVIRON,
    FIXTURES,
    NOW,
    RT,
    FakeRunner,
    listing,
    projection_answer,
    routing_overlay,
)

pytestmark = pytest.mark.unit

REPORT = "exp-triagepow-6800-2026-11-13T2045Z.md"


def _report_text() -> str:
    return (FIXTURES / "reports" / REPORT).read_text(encoding="utf-8")


def _handler(
    runner: FakeRunner, environ: dict[str, str] | None = None
) -> HandlerFloorDelegationHistory:
    return HandlerFloorDelegationHistory(
        RT,
        runner,
        clock=lambda: NOW,
        environ=ENVIRON if environ is None else environ,
        routing_overlay_loader=routing_overlay,
    )


def _run(handler: HandlerFloorDelegationHistory, home: Path):
    return handler.handle(ModelFloorDelegationHistoryRequest(home=str(home)))


def _written(home: Path) -> dict:
    return json.loads((home / RT.output_relpath).read_text())


def _hosts() -> dict[str, tuple[int, str, str]]:
    return {
        "/registry/hosta-reports": (0, listing((REPORT, 1, _report_text())), ""),
        "lab-b": (
            0,
            listing(
                (
                    "exp-seamshot-6799-2026-11-13T1845Z.md",
                    1,
                    (
                        FIXTURES / "reports" / "exp-seamshot-6799-2026-11-13T1845Z.md"
                    ).read_text(),
                )
            ),
            "",
        ),
    }


def test_golden_all_three_sections_are_written_from_the_three_readings(
    tmp_path: Path,
) -> None:
    result = _run(_handler(FakeRunner(reports=_hosts())), tmp_path)
    assert result.ok
    assert (result.models, result.experiments) == (2, 3)
    doc = ModelDelegationHistory.model_validate(_written(tmp_path))
    assert {m.id for m in doc.models} == {"Model-X@192.0.2.10", "glm-flash"}
    assert doc.counts.attempts_in_window == 3 * 4 * 2
    assert {e.id for e in doc.experiments} == {
        "exp-triagepow-6800",
        "exp-seamshot-6799",
        "exp-seamshot-6799#2",
    }
    triage = next(e for e in doc.experiments if e.id == "exp-triagepow-6800")
    assert triage.date == "2026-11-13T20:45:00Z"
    assert triage.date_source == "filename"
    assert triage.source == f"hosta:{REPORT}"
    zero = next(a for a in triage.arms if a.label == "local_zero")
    assert (
        zero.model == "Model-X@192.0.2.10"
    )  # the overlay's arm rule maps the label to a backend
    assert zero.settings is not None
    assert all(s.ok for s in doc.sources)


def test_golden_the_projection_read_is_one_read_only_transaction_with_no_prompt_or_response_text(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(reports=_hosts())
    _run(_handler(runner), tmp_path)
    scripts = [
        " ".join(call) for call in runner.calls if "BEGIN READ ONLY" in " ".join(call)
    ]
    assert len(scripts) == 1
    script = scripts[0]
    assert script.count("BEGIN READ ONLY") == 1
    assert "COMMIT" in script
    assert "prompt_text" not in script
    assert "response_text" not in script
    assert (
        "SELECT *" not in script
    )  # a star would materialise the text columns in the grouped read
    assert "lab-db" in script.split("--")[1] or "lab-db" in " ".join(runner.calls[0])


def test_golden_a_report_on_two_hosts_is_one_experiment(tmp_path: Path) -> None:
    hosts = {
        "/registry/hosta-reports": (0, listing((REPORT, 1, _report_text())), ""),
        "lab-b": (0, listing((REPORT, 2, _report_text())), ""),
    }
    _run(_handler(FakeRunner(reports=hosts)), tmp_path)
    doc = _written(tmp_path)
    assert [e["id"] for e in doc["experiments"]] == ["exp-triagepow-6800"]
    assert doc["experiments"][0]["source"].startswith("hosta:")  # the first source wins


def test_error_a_projection_that_never_answers_keeps_the_previous_file(
    tmp_path: Path,
) -> None:
    ok = _run(_handler(FakeRunner(reports=_hosts())), tmp_path)
    assert ok.ok
    before = (tmp_path / RT.output_relpath).read_bytes()
    broken = _run(
        _handler(FakeRunner(projection=(124, "", "timed out after 40 s"), reports={})),
        tmp_path,
    )
    assert not broken.ok
    assert "previous file kept" in broken.detail
    assert (tmp_path / RT.output_relpath).read_bytes() == before


def test_error_a_projection_that_never_answers_and_no_previous_file_writes_the_failure_into_the_sources(
    tmp_path: Path,
) -> None:
    result = _run(
        _handler(
            FakeRunner(projection=(124, "", "timed out after 40 s"), reports=_hosts())
        ),
        tmp_path,
    )
    assert not result.ok
    doc = _written(tmp_path)
    projection = next(s for s in doc["sources"] if s["id"] == "projection")
    assert projection["ok"] is False
    assert "timed out" in projection["detail"]
    assert (
        doc["series"] == []
    )  # no projection, no series: a missing reading is never a zero
    assert all(
        m["first_seen"] == m["last_seen"] for m in doc["models"]
    )  # only the models the experiments name
    assert len(doc["experiments"]) == 3  # the readings that were made are still served


def test_error_an_answer_without_the_read_marker_is_unparsable_never_an_empty_window(
    tmp_path: Path,
) -> None:
    result = _run(
        _handler(FakeRunner(projection=(0, "A {}\n", ""), reports=_hosts())), tmp_path
    )
    assert not result.ok
    assert (
        "read marker"
        in next(s for s in _written(tmp_path)["sources"] if s["id"] == "projection")[
            "detail"
        ]
    )


def test_error_a_report_host_that_is_down_is_named_and_the_other_host_is_still_served(
    tmp_path: Path,
) -> None:
    hosts = _hosts()
    hosts["lab-b"] = (255, "", "ssh: connect to host lab-b: Connection refused")
    result = _run(_handler(FakeRunner(reports=hosts)), tmp_path)
    assert result.ok
    doc = _written(tmp_path)
    down = next(s for s in doc["sources"] if s["id"] == "reports:hostb")
    assert down["ok"] is False
    assert "Connection refused" in down["detail"]
    assert [e["id"] for e in doc["experiments"]] == ["exp-triagepow-6800"]


def test_error_an_unset_ssh_variable_names_itself(tmp_path: Path) -> None:
    result = _run(_handler(FakeRunner(reports=_hosts()), environ={}), tmp_path)
    assert not result.ok
    projection = next(
        s for s in _written(tmp_path)["sources"] if s["id"] == "projection"
    )
    assert RT.database.ssh_host_env in projection["detail"]


def test_error_a_projection_row_with_no_model_is_skipped_and_counted_never_listed(
    tmp_path: Path,
) -> None:
    answer = (
        projection_answer()
        + 'A {"bucket": "2026-11-14T01:00:00Z", "model_name": "", "backend_id": "", "host": "", "task_type": "x", "attempts": 2, "accepted": 0, "timeouts": 0, "cost_usd": 0}\n'
    )
    _run(_handler(FakeRunner(projection=(0, answer, ""), reports=_hosts())), tmp_path)
    doc = _written(tmp_path)
    projection = next(s for s in doc["sources"] if s["id"] == "projection")
    assert "1 unattributed rows skipped" in projection["detail"]
    assert doc["counts"]["attempts_in_window"] == 24


def test_error_a_broken_routing_overlay_is_named_and_the_settings_stay_unrecorded(
    tmp_path: Path,
) -> None:
    def broken() -> ModelDelegationRoutingOverlay:
        raise RuntimeError("no overlay")

    handler = HandlerFloorDelegationHistory(
        RT,
        FakeRunner(reports=_hosts()),
        clock=lambda: NOW,
        environ=ENVIRON,
        routing_overlay_loader=broken,
    )
    _run(handler, tmp_path)
    doc = _written(tmp_path)
    routing = next(s for s in doc["sources"] if s["id"] == "routing_overlay")
    assert routing["ok"] is False
    assert routing["detail"] == "RuntimeError"
    assert all(m["context_tokens"] is None for m in doc["models"])


def test_error_no_registry_home_is_refused_by_name() -> None:
    result = _handler(FakeRunner(), environ={}).handle(
        ModelFloorDelegationHistoryRequest()
    )
    assert not result.ok
    assert RT.home_env in result.detail
