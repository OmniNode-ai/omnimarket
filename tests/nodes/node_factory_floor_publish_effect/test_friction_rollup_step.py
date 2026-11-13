# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's friction report is built through the rollup node with the overlay home's class rules (OMN-22478).

The report the floor read was a copy of the one the ``friction-rollup`` timer unit writes; that unit passes no class
rules, so rows written with no ``class=`` cell stayed ``unclassified`` however many rules were recorded. The step
``friction_classed`` reads the ledger, runs the rollup node with the rules, and writes the report the floor reads.

Golden chain: a ledger window with rows of every measured family is rolled with the shipped rule set; no row is left
unclassified and the floor's own reader names each class. Error chain: with no rules the same ledger is all
unclassified (the failing control, which is what the copy step gives); a missing overlay or ledger path fails the step
and leaves the previous report in place; a row naming no recorded cause stays unclassified.
"""

from __future__ import annotations

import json
import shlex
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import omnimarket.nodes.node_friction_rollup_compute as rollup_package
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_stoppages
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_friction_rollup_source import (
    HandlerFrictionRollupSource,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelPublishStep,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)
OVERLAY = Path(rollup_package.__file__).parent / "overlay.example.yaml"
OVERLAY_ENV = "ONEX_FRICTION_CLASS_OVERLAY"
READ_ENV = "ONEX_LEDGER_BUS_READ_COMMAND"

# A stand-in for the bus read command that runs the read node's own handler (the real request model, the real paged rows
# answer, the real answer bound) over the rows in argv[1], with the bound set to argv[2] bytes: one typed query JSON on stdin, one
# JSON answer on stdout. Like the ``onex ledger query`` command, an answer the node refuses or errors exits 1. Each request is
# appended to the file in argv[3], so a test reads what the step asked for. A request the node's own model refuses exits 2 with
# its reason (OMN-23606: a request with no correlation id).
FAKE_READ = """
import json, sys
from datetime import UTC, datetime
from omnimarket.models.work_ledger_query import ModelWorkLedgerAnswerBound, ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import HandlerWorkLedgerQuery
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import InMemoryWorkLedgerQueryReader
text = sys.stdin.read()
with open(sys.argv[3], "a", encoding="utf-8") as log:
    log.write(text.replace("\\n", " ") + "\\n")
try:
    request = ModelWorkLedgerQueryRequest.model_validate_json(text)
except ValueError as exc:
    print(f"the request is not a work ledger query: {exc}", file=sys.stderr)
    sys.exit(2)
reader = InMemoryWorkLedgerQueryReader(now=lambda: datetime.now(UTC))
for number, line in enumerate(open(sys.argv[1], encoding="utf-8")):
    reader.ingest({"row_id": f"row-{number}", "raw_row": line.rstrip("\\n")})
node = HandlerWorkLedgerQuery(reader=reader, now=lambda: datetime.now(UTC))
bound = int(sys.argv[2])
node._answer_bound = ModelWorkLedgerAnswerBound(
    broker_max_request_size=bound + 1000, envelope_headroom_bytes=0, max_answer_bytes=bound
)
answer = node.handle(request)
if answer.status != "ok":
    print(f"Work-ledger read refused ({answer.status}): {answer.error or answer.refusal}", file=sys.stderr)
    sys.exit(1)
print(answer.model_dump_json())
"""

# The answer bound the stand-in enforces: the whole planted window does not fit under it, a page of a few rows does.
TIGHT_BOUND = 9000
LOOSE_BOUND = 917516

# One phrase of each measured row's own text, and the class the shipped rules must give the row that carries it.
MEASURED = {
    "lab-run-bus-unit-no-receipt": "the lab-run bus unit sent to a host returned no receipt",
    "pr-handoff-install-first-live-read-refused": "pr-handoff refused overlays#13 with exit 5: the install-first check could not read the PR",
    "github-gateway-read-refused": "Every onex scm gateway read refuses with ModelGithubGatewayRequest cache_only",
    "ticket-create-gate-lane-identity-from-sidecar": "The create gate named the lane from its harness sidecar and refused with lane_identity_missing",
    "remote-lane-ledger-relay-queued": "every row it writes returns relay=queued and reaches the ledger only at the end",
    "lab-stack-reaper-unleased-stack": "omnibase-infra-local: unleased stack; ownership cannot be proved",
    "floor-publisher-installed-from-several-branches": "Three lanes installed the floor publisher from three branches within 90 seconds",
    "baselined-script-ratchet-refused-fix": "SHRINK-ONLY RATCHET REFUSED A FIX TO A BASELINED SCRIPT",
    "zero-byte-index-lock": "clone sync failed twice on a zero-byte .git/index.lock that no process holds",
    "launchd-bootstrap-fails-after-bootout": 'printed "Bootstrap failed: 5: Input/output error" and left the unit unloaded',
    "step-timed-out-on-loaded-mac": "pr-create ran past 130 s with no output while the Mac load average was 35",
    "step-longer-than-its-time-limit": "record.sh took 192.2 s and timed out twice at the forked executor's tool limit",
    "deploy-runners-route-gap": "deploy-runners.sh has no declared route that recreates an online runner",
    "shared-scratchpad-overwrite": "SHARED SCRATCHPAD AND TEXT-MATCHING HEAVY-WORK GUARD COST THIS LANE TIME",
    "plan-and-ledger-read-tool-gaps": "No runtime_probe tool, so no row can reach DEPLOYED",
    "dev-redeploy-blocked-by-build-settings": "dev_lane_build_settings refuses every runtime and full deploy job",
    "landing-worker-timing-tests-flaky": "validator-tests failed with two timing tests unrelated to it",
    "code-delegation-every-route-failed": "The receipted code delegation (delegation_step.py code) failed on every route",
}
UNMATCHED = "A hand-written row whose text names none of the recorded causes."


def row(minute: int, lane: str, text: str, *, cls: str | None = None) -> str:
    stamp = (NOW - timedelta(hours=2, minutes=-minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = [
        stamp,
        "FRICTION",
        f"lane={lane}",
        "ticket=OMN-22478",
        "cost=~10 lane-minutes",
    ]
    if cls:
        cells.append(f"class={cls}")
    return " | ".join([*cells, text])


def status_row(minute: int, lane: str, text: str) -> str:
    stamp = (NOW - timedelta(hours=2, minutes=-minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | STATUS | lane={lane} | ticket=OMN-22478 | {text}"


def ledger_text() -> str:
    rows = [row(i, f"lane-{i}", text) for i, text in enumerate(MEASURED.values())]
    rows.append(row(40, "lane-unmatched", UNMATCHED))
    rows.append(
        row(41, "lane-written", "A writer that named its class.", cls="tick-liveness")
    )
    # STATUS rows the floor does not draw (most of the ledger's volume), and the one that resolves a class.
    rows.extend(
        status_row(m, f"lane-s{m}", "a status line " + "x" * 300) for m in range(50, 90)
    )
    rows.append(status_row(95, "alarm", "healthy again | resolved=tick-liveness"))
    return "\n".join(rows) + "\n"


def environ(
    tmp_path: Path, overlay: Path | None = OVERLAY, *, bound: int = LOOSE_BOUND
) -> dict[str, str]:
    """The environment that routes the ledger reads through a real read command over the planted rows."""
    rows = tmp_path / "bus" / "planted_rows.txt"
    rows.parent.mkdir(parents=True, exist_ok=True)
    rows.write_text(ledger_text(), encoding="utf-8")
    script = rows.parent / "fake_read.py"
    script.write_text(FAKE_READ, encoding="utf-8")
    env = {
        READ_ENV: f"{sys.executable} {script} {rows} {bound} {request_log(tmp_path)}"
    }
    if overlay is not None:
        env[OVERLAY_ENV] = str(overlay)
    return env


def request_log(tmp_path: Path) -> Path:
    return tmp_path / "bus" / "requests.log"


def requests_sent(tmp_path: Path) -> list[dict[str, object]]:
    log = request_log(tmp_path)
    return (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


def friction_step(**rollup: object) -> ModelPublishStep:
    """The contract's friction step, with the named settings of its rollup block replaced (a test's own page size or budget)."""
    step = next(
        s
        for s in load_runtime().with_limits(TEST_LIMITS).steps
        if s.name == "friction_classed"
    )
    if not rollup:
        return step
    assert step.rollup is not None
    return step.model_copy(update={"rollup": step.rollup.model_copy(update=rollup)})


def run_step(
    tmp_path: Path, env: dict[str, str], *, step: ModelPublishStep | None = None
) -> tuple[str, str, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    step = step if step is not None else friction_step()
    handler = HandlerFactoryFloorPublish(wall_clock=lambda: NOW)
    result = handler._run_step(
        step,
        home,
        ModelFactoryFloorPublishOverlay(
            limits=TEST_LIMITS,
            ssh_target="lab-web",
            remote_dir=str(home / "site"),
            serve_url="http://example.invalid:8088/",
            bus_lane="lane-x",
            history_root=str(home / "kept"),
            owner_login="owner@example.invalid",
        ),
        "CLAUDE_PLUGIN_ROOT",
        env,
    )
    assert step.output_relpath is not None
    return (
        result.status,
        f"{result.detail} {result.diagnostic}".strip(),
        home / step.output_relpath,
    )


def classes_of(report_path: Path) -> dict[str, int]:
    return {s.name: s.occurrences for s in load_stoppages(report_path)}


def test_golden_every_measured_family_is_named_and_the_floors_reader_sees_each_class(
    tmp_path: Path,
) -> None:
    status, detail, path = run_step(tmp_path, environ(tmp_path))
    assert (status, detail) == ("ok", "")
    seen = classes_of(path)
    assert "unclassified" in seen
    assert seen["unclassified"] == 1  # only the row that names no recorded cause
    for name in MEASURED:
        assert seen.get(name) == 1, name
    assert seen["tick-liveness"] == 1
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["totals"]["rows_classified_by_rule"] == len(MEASURED)


def test_golden_the_contract_step_names_the_overlay_and_every_setting_it_reads() -> (
    None
):
    step = friction_step()
    assert step.kind == "friction_rollup"
    assert step.required is False
    assert step.concurrent is True  # inside the 255 s cycle bound
    rollup = step.rollup
    assert rollup is not None
    assert rollup.class_overlay_id == "node_friction_rollup_compute"
    assert (rollup.window_hours, rollup.bucket_hours) == (24, 2)
    names = [s.name for s in load_runtime().with_limits(TEST_LIMITS).steps]
    assert names.index("friction_classed") > names.index("friction")


def test_golden_the_shipped_overlay_names_every_measured_class() -> None:
    declared = {r["name"] for r in yaml.safe_load(OVERLAY.read_text())["class_rules"]}
    assert set(MEASURED) <= declared


def test_error_control_with_no_rules_every_row_is_unclassified(tmp_path: Path) -> None:
    empty = tmp_path / "empty-overlay.yaml"
    empty.write_text("class_rules: []\n", encoding="utf-8")
    status, _detail, path = run_step(tmp_path, environ(tmp_path, empty))
    assert status == "ok"
    seen = classes_of(path)
    assert seen["unclassified"] == len(MEASURED) + 1
    assert not set(MEASURED) & set(seen)


def test_error_a_row_that_names_no_recorded_cause_stays_unclassified(
    tmp_path: Path,
) -> None:
    _status, _detail, path = run_step(tmp_path, environ(tmp_path))
    report = json.loads(path.read_text(encoding="utf-8"))
    unclassified = next(c for c in report["classes"] if c["class"] == "unclassified")
    assert unclassified["lanes"] == ["lane-unmatched"]


def test_error_a_missing_overlay_fails_the_step_and_keeps_the_previous_report(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    step = friction_step()
    assert step.output_relpath is not None
    previous = home / step.output_relpath
    previous.parent.mkdir(parents=True)
    previous.write_text('{"classes": []}', encoding="utf-8")
    status, detail, _ = run_step(
        tmp_path, {**environ(tmp_path, None), "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    )
    assert status == "failed"
    assert "NodeOverlayError" in detail or "overlay" in detail.lower()
    assert previous.read_text(encoding="utf-8") == '{"classes": []}'


def test_error_an_unset_read_command_refuses_with_no_default(tmp_path: Path) -> None:
    env = environ(tmp_path)
    del env[READ_ENV]
    status, detail, _ = run_step(tmp_path, env)
    assert status == "failed"
    assert READ_ENV in detail
    assert "PublishRefusedError" in detail


def test_error_a_failed_bus_read_refuses_and_keeps_the_previous_report(
    tmp_path: Path,
) -> None:
    env = environ(tmp_path)
    env[READ_ENV] = f"{sys.executable} -c 'import sys; sys.exit(7)'"
    step = friction_step()
    assert step.output_relpath is not None
    previous = tmp_path / "home" / step.output_relpath
    previous.parent.mkdir(parents=True)
    previous.write_text('{"classes": ["previous"]}', encoding="utf-8")
    status, detail, _ = run_step(tmp_path, env)
    assert status == "failed"
    assert "read-command-exit-7" in detail
    assert previous.read_text(encoding="utf-8") == '{"classes": ["previous"]}'


def test_golden_every_bus_read_carries_a_correlation_id_the_read_node_accepts(
    tmp_path: Path,
) -> None:
    """OMN-23606: the read node's request model requires a UUID correlation id (the stand-in validates each request as that
    model and exits 2 on one that fails it). The step sent none, so every read exited 2 and the floor's friction step failed
    on every cycle; the contract's default command mints an id itself and hid the gap."""
    status, detail, path = run_step(tmp_path, environ(tmp_path))
    assert (status, detail) == ("ok", "")
    assert classes_of(path)["tick-liveness"] == 1
    sent = requests_sent(tmp_path)
    assert sent
    assert all(request["correlation_id"] for request in sent)


def test_golden_the_step_asks_for_only_the_rows_the_floor_draws_one_page_at_a_time(
    tmp_path: Path,
) -> None:
    status, detail, path = run_step(
        tmp_path, environ(tmp_path), step=friction_step(ledger_page_size=7)
    )
    assert (status, detail) == ("ok", "")
    sent = requests_sent(tmp_path)
    # 20 measured FRICTION rows + 2 = 20 rows at 7 a page is 3 pages; the one resolved= STATUS row is one page.
    assert [(r["kinds"], r.get("term")) for r in sent] == [
        (["FRICTION"], None),
        (["FRICTION"], None),
        (["FRICTION"], None),
        (["STATUS"], "resolved="),
    ]
    assert all(r["page_size"] == 7 and r["query"] == "rows" for r in sent)
    assert "cursor" not in sent[0]
    assert sent[1]["cursor"]
    assert sent[2]["cursor"]
    assert "cursor" not in sent[3]
    seen = classes_of(path)
    assert seen["unclassified"] == 1
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["totals"]["friction_rows"] == len(MEASURED) + 2
    tick = next(c for c in report["classes"] if c["class"] == "tick-liveness")
    assert tick["resolved_at"]  # the resolved= STATUS row reached the rollup


def test_golden_the_signals_block_is_written_unread_never_as_counts_of_rows_not_read(
    tmp_path: Path,
) -> None:
    _status, _detail, path = run_step(tmp_path, environ(tmp_path))
    signals = json.loads(path.read_text(encoding="utf-8"))["signals"]
    assert signals["read"] is False
    assert "CORRECTION" in signals["reason"]


def test_control_an_answer_over_the_nodes_bound_refuses_the_old_window_read_and_the_paged_step_completes(
    tmp_path: Path,
) -> None:
    """The failing control of OMN-23606. Under a bound the planted window does not fit, a read of the whole window as the
    step used to send it (every kind, a row limit, no page) is refused by the node itself, and the paged step still reads the
    same window to the end."""
    env = environ(tmp_path, bound=TIGHT_BOUND)
    old_route = CommandLedgerRowSource(shlex.split(env[READ_ENV]), 60, 5000)
    refused = old_route.read(
        ModelLedgerQuery(
            since=NOW - timedelta(hours=24),
            until=NOW,
            row_type="STATUS",
            correlation_id=str(uuid.uuid4()),
        )
    )
    assert refused.rows == ()
    assert refused.error is not None
    assert refused.error.startswith("read-command-exit-1")

    status, detail, path = run_step(
        tmp_path, env, step=friction_step(ledger_page_size=5)
    )
    assert (status, detail) == ("ok", "")
    assert classes_of(path)["tick-liveness"] == 1


def test_error_a_page_the_node_refuses_as_too_large_is_unread_and_keeps_the_previous_report(
    tmp_path: Path,
) -> None:
    step = friction_step(
        ledger_page_size=200
    )  # 200 rows of the planted STATUS text do not fit under the tight bound
    assert step.output_relpath is not None
    previous = tmp_path / "home" / step.output_relpath
    previous.parent.mkdir(parents=True)
    previous.write_text('{"classes": ["previous"]}', encoding="utf-8")
    status, detail, _ = run_step(tmp_path, environ(tmp_path, bound=1500), step=step)
    assert status == "failed"
    assert "is unread" in detail
    assert "read-command-exit-1" in detail
    assert previous.read_text(encoding="utf-8") == '{"classes": ["previous"]}'


def test_error_a_cursor_still_open_after_the_declared_page_count_is_unread_never_zero_friction(
    tmp_path: Path,
) -> None:
    step = friction_step(ledger_page_size=3, ledger_max_pages=2)
    assert step.output_relpath is not None
    status, detail, path = run_step(tmp_path, environ(tmp_path), step=step)
    assert status == "failed"
    assert "2 pages and the cursor is still open" in detail
    assert not path.exists()  # no report from the pages that did arrive


def test_error_a_spent_time_budget_is_unread_and_stops_reading(
    tmp_path: Path,
) -> None:
    step = friction_step(ledger_page_size=3, ledger_time_budget_seconds=5)
    ticks = iter([0.0, 1.0, 2.0, 6.0, 7.0, 8.0])
    home = tmp_path / "home"
    home.mkdir()
    assert step.rollup is not None
    with pytest.raises(PublishRefusedError) as raised:
        HandlerFrictionRollupSource().handle(
            step.rollup,
            now=NOW,
            environ=environ(tmp_path),
            monotonic=lambda: next(ticks),
        )
    assert "5 s time budget ended before page 3" in str(raised.value)
    assert len(requests_sent(tmp_path)) == 2


def test_error_a_page_runs_under_the_time_left_of_the_budget() -> None:
    """The last page may not outlive the budget: its timeout is cut to what is left, rounded up to a whole second."""
    seen: list[int] = []

    class Spy:
        def __init__(self, argv: list[str], timeout: int) -> None:
            seen.append(timeout)

        def read(self, query: ModelPointQuery) -> ModelPointAnswer:
            return ModelPointAnswer(lines=())

    step = friction_step(ledger_time_budget_seconds=80)
    ticks = iter([0.0, 5.0, 45.5, 50.0, 60.0])
    assert step.rollup is not None
    with patch(
        "omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_friction_rollup_source.CommandPointRead",
        Spy,
    ):
        HandlerFrictionRollupSource().handle(
            step.rollup,
            now=NOW,
            environ={READ_ENV: "true", OVERLAY_ENV: str(OVERLAY)},
            monotonic=lambda: next(ticks),
        )
    assert seen == [60, 35]  # 75 s left: the declared page timeout; 34.5 s left: 35 s


def test_golden_the_contract_declares_every_paging_value_and_they_fit_the_step_limit() -> (
    None
):
    step = friction_step()
    assert step.rollup is not None
    rollup = step.rollup
    assert rollup.ledger_page_size >= 1
    assert rollup.ledger_max_pages >= 1
    assert [read.kinds for read in rollup.ledger_reads] == [("FRICTION",), ("STATUS",)]
    assert rollup.ledger_time_budget_seconds < step.timeout_seconds
    assert rollup.ledger_page_timeout_seconds <= rollup.ledger_time_budget_seconds


def test_error_an_overlay_with_a_pattern_that_does_not_compile_fails_the_step(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad-overlay.yaml"
    bad.write_text(
        'class_rules:\n  - {name: broken, owner: someone, pattern: "(unclosed"}\n',
        encoding="utf-8",
    )
    status, _detail, _ = run_step(tmp_path, environ(tmp_path, bad))
    assert status == "failed"
