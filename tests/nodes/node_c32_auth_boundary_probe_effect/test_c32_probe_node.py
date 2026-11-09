# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of node_c32_auth_boundary_probe_effect (OMN-23213).

Golden: a prober whose five cases and secret search all hold, run by the handler, concludes ``success`` with verdict PASS and
puts one run on the node's result topic; the MVP launch block (M1), given that run as C32's newest scheduled run, reads C32
WORKING. Error: each way the run can be red (a failed clause, an absent surface, a probe that grades nothing, a probe that
raises, a bus that refuses) concludes ``failure``, is emitted where the bus accepts it, and reads C32 NOT WORKING in M1.
Named injected break: ``stale_token_accepted`` plants a dashboard that serves a stale token; the run must read FAIL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_c32_auth_boundary_probe_effect import (
    runtime_c32_probe_tick as tick,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers import (
    handler_c32_auth_boundary_probe as hcp,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.adapter_c32_probers import (
    InMemoryC32Prober,
    InMemoryC32Publisher,
    LocalOnexC32Prober,
    installed_versions,
    reading_from_probe,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.probe_base import (
    Clause,
    Precondition,
    ProbeResult,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.probe_c32 import (
    AUTH_CASES,
    SECRET_CLAUSE,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    EnumC32Verdict,
    ModelC32Clause,
    ModelC32Precondition,
    ModelC32ProbeOverlay,
    ModelC32ProbeReading,
    ModelC32ProbeRequest,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_sources import (
    ModelMvpRun,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_mvp_launch import (
    HandlerMvpLaunch,
)
from tests.nodes.node_floor_simple_effect.mvp_support import (
    MVP_NOW,
    with_history,
)
from tests.nodes.node_floor_simple_effect.mvp_support import (
    request as mvp_request,
)

pytestmark = pytest.mark.unit
NODE = "node_c32_auth_boundary_probe_effect"
TOPIC = "onex.evt.omnimarket.local-auth-boundary-probe-completed.v1"
RAN_AT = datetime(2026, 11, 8, 12, 0, 20, tzinfo=UTC)
NODE_DIR = Path(hcp.__file__).parent.parent
# The version the launch candidate fixture (tests.nodes.node_floor_simple_effect.mvp_support) names for the graded package.
GRADED = {"onex": "1.2.3"}


def _all_clauses(*, stale_token_accepted: bool = False) -> tuple[ModelC32Clause, ...]:
    clauses = [
        ModelC32Clause(name=name, ok=True, detail="held")
        for name, _expect_served in AUTH_CASES
    ]
    if stale_token_accepted:
        # The named injected break: the dashboard served the stale token (case 2 is the second case).
        clauses[1] = ModelC32Clause(
            name=AUTH_CASES[1][0], ok=False, detail="stale token answered 200"
        )
    clauses.append(ModelC32Clause(name=SECRET_CLAUSE, ok=True, detail="held"))
    return tuple(clauses)


def _handler(
    reading: ModelC32ProbeReading | Exception,
    publisher: InMemoryC32Publisher | None = None,
    profile: str = "local",
) -> tuple[hcp.HandlerC32AuthBoundaryProbe, InMemoryC32Publisher]:
    publisher = publisher or InMemoryC32Publisher()

    class _Prober:
        def run_probe(self) -> ModelC32ProbeReading:
            if isinstance(reading, Exception):
                raise reading
            return reading

    handler = hcp.HandlerC32AuthBoundaryProbe(
        prober=_Prober(),
        publisher=publisher,
        topic=TOPIC,
        profile=profile,
        clock=lambda: RAN_AT,
        run_id_factory=lambda _t: "c32-run-1",
    )
    return handler, publisher


def _c32_reading_in_m1(result_mvp_run: dict[str, object]) -> tuple[str, str]:
    run = ModelMvpRun.model_validate(result_mvp_run)
    block = HandlerMvpLaunch().handle(
        mvp_request(with_history("C32", [run]), now=MVP_NOW)
    )
    reading = next(r for r in block.readings if r.id == "C32")
    return reading.state, reading.reason


def test_the_contract_names_the_handler_topic_and_overlay() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["handler"]["class"] == "HandlerC32AuthBoundaryProbe"
    assert contract["event_bus"]["publish_topics"] == [TOPIC]
    assert contract["terminal_event"] == TOPIC
    example = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    overlay = ModelC32ProbeOverlay.model_validate(example)
    assert overlay.default_profile == "inmemory"
    assert overlay.profile("local").kind == "local"
    assert overlay.result_topic in hcp.contract_topics()


def test_an_overlay_missing_a_key_refuses_and_names_it() -> None:
    with pytest.raises(Exception, match="history_keep"):
        ModelC32ProbeOverlay.model_validate(
            {
                "default_profile": "inmemory",
                "profiles": {"inmemory": {"kind": "inmemory"}},
                "history_file": "h.jsonl",
                "result_topic": TOPIC,
            }
        )


def test_a_local_profile_without_graded_packages_refuses() -> None:
    with pytest.raises(Exception, match="graded_packages"):
        ModelC32ProbeOverlay.model_validate(
            {
                "default_profile": "local",
                "profiles": {"local": {"kind": "local", "onex_bin_dir": "/x"}},
                "history_file": "h.jsonl",
                "history_keep": 3,
                "result_topic": TOPIC,
            }
        )


def test_a_local_profile_without_its_onex_directory_refuses() -> None:
    with pytest.raises(Exception, match="onex_bin_dir"):
        ModelC32ProbeOverlay.model_validate(
            {
                "default_profile": "local",
                "profiles": {"local": {"kind": "local", "graded_packages": ["a"]}},
                "history_file": "h.jsonl",
                "history_keep": 3,
                "result_topic": TOPIC,
            }
        )


def test_golden_a_passing_probe_concludes_success_publishes_and_reads_working_in_m1() -> (
    None
):
    handler, publisher = _handler(
        ModelC32ProbeReading(clauses=_all_clauses(), versions=GRADED)
    )
    result = handler.handle(
        ModelC32ProbeRequest(correlation_id="corr-1", trigger="schedule")
    )
    assert result.verdict is EnumC32Verdict.PASS
    assert (result.status, result.conclusion, result.event) == (
        "completed",
        "success",
        "schedule",
    )
    assert result.emitted is True
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event["topic"] == TOPIC
    assert event["partition_key"] == "C32"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["criterion"] == "C32"
    assert payload["mvp_run"] == result.mvp_run()
    state, reason = _c32_reading_in_m1(payload["mvp_run"])
    assert state == "WORKING", reason


@pytest.mark.parametrize(
    ("reading", "verdict"),
    [
        (
            ModelC32ProbeReading(clauses=_all_clauses(stale_token_accepted=True)),
            EnumC32Verdict.FAIL,
        ),
        (
            ModelC32ProbeReading(
                preconditions=(
                    ModelC32Precondition(
                        name="`onex dashboard` subcommand",
                        present=False,
                        detail="absent",
                        owner="OMN-19976",
                    ),
                )
            ),
            EnumC32Verdict.PRECONDITION_ABSENT,
        ),
        (ModelC32ProbeReading(), EnumC32Verdict.ERROR),
        (RuntimeError("dashboard did not start"), EnumC32Verdict.ERROR),
    ],
    ids=["stale_token_accepted", "surface_absent", "graded_nothing", "probe_raised"],
)
def test_error_every_red_run_concludes_failure_is_published_and_reads_not_working_in_m1(
    reading: ModelC32ProbeReading | Exception, verdict: EnumC32Verdict
) -> None:
    handler, publisher = _handler(reading)
    result = handler.handle(ModelC32ProbeRequest(correlation_id="corr-2"))
    assert result.verdict is verdict
    assert result.conclusion == "failure"
    assert result.emitted is True
    assert len(publisher.events) == 1
    if verdict is EnumC32Verdict.ERROR:
        assert result.status == "errored"
        assert result.error
    state, reason = _c32_reading_in_m1(result.mvp_run())
    assert state == "NOT WORKING", reason


def test_error_a_green_run_on_other_versions_than_the_candidate_reads_not_working_in_m1() -> (
    None
):
    handler, _ = _handler(
        ModelC32ProbeReading(clauses=_all_clauses(), versions={"onex": "0.0.1"})
    )
    result = handler.handle(ModelC32ProbeRequest(correlation_id="corr-5"))
    assert result.conclusion == "success"
    assert _c32_reading_in_m1(result.mvp_run()) == ("NOT WORKING", "not-the-candidate")


def test_error_a_green_run_that_recorded_no_versions_reads_not_working_in_m1() -> None:
    handler, _ = _handler(ModelC32ProbeReading(clauses=_all_clauses()))
    result = handler.handle(ModelC32ProbeRequest(correlation_id="corr-6"))
    assert _c32_reading_in_m1(result.mvp_run()) == ("NOT WORKING", "not-the-candidate")


def test_error_a_bus_that_refuses_the_run_is_reported_unpublished() -> None:
    handler, publisher = _handler(
        ModelC32ProbeReading(clauses=_all_clauses()), InMemoryC32Publisher(accept=False)
    )
    result = handler.handle(ModelC32ProbeRequest(correlation_id="corr-3"))
    assert result.conclusion == "success"
    assert result.emitted is False
    assert publisher.events == []


def test_the_inmemory_profile_grades_nothing_and_cannot_read_green() -> None:
    handler, _ = _handler(InMemoryC32Prober().run_probe(), profile="inmemory")
    result = handler.handle(ModelC32ProbeRequest(correlation_id="corr-4"))
    assert result.verdict is EnumC32Verdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"


def test_a_probe_result_converts_clause_for_clause_and_the_probe_verdict_agrees() -> (
    None
):
    probe = ProbeResult(criterion="C32")
    probe.preconditions.append(Precondition("onex CLI", True, "found", "OMN-19915"))
    probe.clauses.append(Clause("a", True, "ok"))
    probe.clauses.append(Clause("b", False, "no"))
    reading = reading_from_probe(probe)
    assert reading.verdict() is EnumC32Verdict.FAIL
    assert [(c.name, c.ok) for c in reading.clauses] == [("a", True), ("b", False)]


def test_the_local_prober_puts_the_overlay_directory_first_on_path_and_restores_it() -> (
    None
):
    seen: list[str] = []
    environ = {"PATH": "/usr/bin"}

    def _run() -> ProbeResult:
        seen.append(environ["PATH"])
        probe = ProbeResult(criterion="C32")
        probe.clauses.append(Clause("a", True, "ok"))
        return probe

    reading = LocalOnexC32Prober(
        "/venv/bin", run=_run, versions=lambda: {"onex": "1.2.3"}, environ=environ
    ).run_probe()
    assert seen == ["/venv/bin:/usr/bin"]
    assert environ["PATH"] == "/usr/bin"
    assert reading.versions == {"onex": "1.2.3"}


def test_installed_versions_reads_the_environment_it_is_pointed_at_and_omits_an_absent_package() -> (
    None
):
    import sys

    found = installed_versions(
        str(Path(sys.executable).parent), ("pydantic", "no-such-package-c32")
    )
    assert set(found) == {"pydantic"}
    assert found["pydantic"]
    assert installed_versions("/no/such/bin", ("pydantic",)) == {}


# ----------------------------------------------------------------------------------------------- the tick


def _overlay_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **over: object
) -> None:
    body = {
        "default_profile": "inmemory",
        "profiles": {"inmemory": {"kind": "inmemory"}},
        "history_file": "c32-runs.jsonl",
        "history_keep": 2,
        "result_topic": TOPIC,
        **over,
    }
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(body))
    monkeypatch.setenv(hcp.OVERLAY_FILE_ENV, str(path))


def _tick(tmp_path: Path, handler: hcp.HandlerC32AuthBoundaryProbe | None) -> int:
    return tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        handler=handler,
        now=RAN_AT,
    )


def test_golden_tick_publishes_writes_the_evidence_and_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_file(tmp_path, monkeypatch)
    handler, publisher = _handler(
        ModelC32ProbeReading(clauses=_all_clauses(), versions=GRADED)
    )
    assert _tick(tmp_path, handler) == 0
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert evidence["verdict"] == "PASS"
    assert evidence["emitted"] is True
    assert evidence["clauses_ok"] == evidence["clauses"] == len(AUTH_CASES) + 1
    history = (tmp_path / "state" / "c32-runs.jsonl").read_text().splitlines()
    assert [json.loads(h)["conclusion"] for h in history] == ["success"]
    assert len(publisher.events) == 1


def test_the_history_keeps_only_the_newest_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_file(tmp_path, monkeypatch)
    for n in range(3):
        handler, _ = _handler(ModelC32ProbeReading(clauses=_all_clauses()))
        handler._run_id = lambda _t, n=n: f"c32-run-{n}"
        _tick(tmp_path, handler)
    history = (tmp_path / "state" / "c32-runs.jsonl").read_text().splitlines()
    assert [json.loads(h)["run_id"] for h in history] == ["c32-run-1", "c32-run-2"]


def test_error_tick_with_a_failing_probe_exits_1_and_the_evidence_names_the_clause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_file(tmp_path, monkeypatch)
    handler, publisher = _handler(
        ModelC32ProbeReading(clauses=_all_clauses(stale_token_accepted=True))
    )
    assert _tick(tmp_path, handler) == tick.EXIT_FAILED
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert evidence["verdict"] == "FAIL"
    assert evidence["failed_clauses"] == [AUTH_CASES[1][0]]
    assert len(publisher.events) == 1


def test_error_tick_whose_run_the_bus_refuses_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_file(tmp_path, monkeypatch)
    handler, _ = _handler(
        ModelC32ProbeReading(clauses=_all_clauses()), InMemoryC32Publisher(accept=False)
    )
    assert _tick(tmp_path, handler) == tick.EXIT_NOT_PUBLISHED


def test_error_tick_with_no_overlay_exits_2_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(hcp.OVERLAY_FILE_ENV, raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    assert _tick(tmp_path, None) == tick.EXIT_OVERLAY
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert evidence["status"] == "overlay_unreadable"
    assert evidence["error"]


def test_error_tick_with_a_result_topic_the_contract_does_not_declare_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_file(
        tmp_path, monkeypatch, result_topic="onex.evt.omnimarket.elsewhere.v1"
    )
    assert _tick(tmp_path, None) == tick.EXIT_OVERLAY


# ----------------------------------------------------------------------------------------------- the unit


def test_the_host_installer_declares_a_daily_systemd_unit_for_the_tick() -> None:
    from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
        load_units,
    )

    unit = load_units()["c32-auth-boundary-probe"]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 86400
    assert unit["schedule"]["run_timeout_s"] <= 3 * unit["schedule"]["start_interval"]
    assert unit["module"].endswith(f"{NODE}.runtime_c32_probe_tick")
    assert (NODE_DIR.parent.parent.parent.parent / unit["entry_file"]).is_file()


def test_a_tick_profile_grades_the_environment_the_tick_runs_in_and_names_no_directory() -> (
    None
):
    import sys

    overlay = ModelC32ProbeOverlay.model_validate(
        {
            "default_profile": "tick",
            "profiles": {"tick": {"kind": "tick", "graded_packages": ["pydantic"]}},
            "history_file": "h.jsonl",
            "history_keep": 3,
            "result_topic": TOPIC,
        }
    )
    prober = hcp.build_prober(overlay, None)
    assert isinstance(prober, LocalOnexC32Prober)
    assert prober._bin_dir == str(Path(sys.executable).parent)
    with pytest.raises(Exception, match="onex_bin_dir"):
        ModelC32ProbeOverlay.model_validate(
            {
                "default_profile": "tick",
                "profiles": {
                    "tick": {
                        "kind": "tick",
                        "onex_bin_dir": "/x",
                        "graded_packages": ["a"],
                    }
                },
                "history_file": "h.jsonl",
                "history_keep": 3,
                "result_topic": TOPIC,
            }
        )
