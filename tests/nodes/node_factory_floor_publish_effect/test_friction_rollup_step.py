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
LEDGER_ENV = "ONEX_LEDGER_PATH"

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


def environ(tmp_path: Path, overlay: Path | None = OVERLAY) -> dict[str, str]:
    ledger = tmp_path / "ledger" / "ROLLING_WORK_LEDGER.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(ledger_text(), encoding="utf-8")
    env = {LEDGER_ENV: str(ledger)}
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
    return result.status, result.detail, home / step.output_relpath


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


def test_error_an_unset_ledger_path_refuses_with_no_default(tmp_path: Path) -> None:
    env = environ(tmp_path)
    del env[LEDGER_ENV]
    status, detail, _ = run_step(tmp_path, env)
    assert status == "failed"
    assert LEDGER_ENV in detail or "PublishRefusedError" in detail


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
