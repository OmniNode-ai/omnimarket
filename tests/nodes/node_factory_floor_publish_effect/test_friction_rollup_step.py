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
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_friction_rollup_compute as rollup_package
from omnimarket.nodes.node_factory_floor_effect.source_loader import load_stoppages
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelPublishStep,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)
OVERLAY = Path(rollup_package.__file__).parent / "overlay.example.yaml"
OVERLAY_ENV = "ONEX_FRICTION_CLASS_OVERLAY"
READ_ENV = "ONEX_LEDGER_BUS_READ_COMMAND"

# A stand-in for the bus read command: one typed rows query on stdin, one JSON answer on stdout, from the rows in argv[1].
# ``TRUNCATE`` in argv[2] answers with a cursor, as a bus read does when more rows matched than it returned. ``STRICT`` in argv[2]
# validates the request as the read node's own request model, the way the internal ledger source does, and exits 2 on a request
# that model refuses (OMN-23606: a request with no correlation id).
FAKE_READ = """
import json, sys
from datetime import datetime
text = sys.stdin.read()
if len(sys.argv) > 2 and sys.argv[2] == "STRICT":
    from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
    try:
        ModelWorkLedgerQueryRequest.model_validate_json(text)
    except ValueError as exc:
        print(f"the request is not a work ledger query: {exc}", file=sys.stderr)
        sys.exit(2)
request = json.loads(text)
def when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
since, until = when(request["since"]), when(request["until"])
shown = []
for line in open(sys.argv[1], encoding="utf-8"):
    cells = line.split(" | ")
    if len(cells) < 2 or cells[1] not in request["kinds"]:
        continue
    if since <= when(cells[0]) < until:
        shown.append({"raw_row": line.rstrip("\\n"), "projected_at": cells[0]})
answer = {"status": "ok", "matched": len(shown), "rows": shown[: request["limit"]]}
if len(sys.argv) > 2 and sys.argv[2] == "TRUNCATE":
    answer["next_cursor"] = "more"
print(json.dumps(answer))
"""

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


def ledger_text() -> str:
    rows = [row(i, f"lane-{i}", text) for i, text in enumerate(MEASURED.values())]
    rows.append(row(40, "lane-unmatched", UNMATCHED))
    rows.append(
        row(41, "lane-written", "A writer that named its class.", cls="tick-liveness")
    )
    return "\n".join(rows) + "\n"


def environ(
    tmp_path: Path, overlay: Path | None = OVERLAY, *, read_tail: str = ""
) -> dict[str, str]:
    """The environment that routes the ledger reads through a real read command over the planted rows."""
    rows = tmp_path / "bus" / "planted_rows.txt"
    rows.parent.mkdir(parents=True, exist_ok=True)
    rows.write_text(ledger_text(), encoding="utf-8")
    script = rows.parent / "fake_read.py"
    script.write_text(FAKE_READ, encoding="utf-8")
    env = {READ_ENV: f"{sys.executable} {script} {rows} {read_tail}".strip()}
    if overlay is not None:
        env[OVERLAY_ENV] = str(overlay)
    return env


def friction_step() -> ModelPublishStep:
    return next(s for s in load_runtime().steps if s.name == "friction_classed")


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
    assert step.concurrent is True  # inside the 240 s cycle bound
    rollup = step.rollup
    assert rollup is not None
    assert rollup.class_overlay_id == "node_friction_rollup_compute"
    assert (rollup.window_hours, rollup.bucket_hours) == (24, 2)
    names = [s.name for s in load_runtime().steps]
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
    """OMN-23606: the internal ledger source validates each request as the read node's request model, which requires a UUID
    correlation id. The step sent none, so every read exited 2 and the floor's friction step failed on every cycle; the
    contract's default command mints an id itself and hid the gap."""
    status, detail, path = run_step(tmp_path, environ(tmp_path, read_tail="STRICT"))
    assert (status, detail) == ("ok", "")
    assert classes_of(path)["tick-liveness"] == 1


def test_error_a_truncated_bus_read_is_refused_never_rolled_short(
    tmp_path: Path,
) -> None:
    status, detail, _ = run_step(tmp_path, environ(tmp_path, read_tail="TRUNCATE"))
    assert status == "failed"
    assert "read-answer-truncated" in detail


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
