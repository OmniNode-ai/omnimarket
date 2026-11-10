# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host installer's start proof for the landing snapshot step (OMN-23458).

``python -m omnimarket.nodes.node_pr_landing_snapshot_effect --help`` must be a pure start check:
argparse answers with the usage and exits 0 before any overlay is read, so the check prints no
overlay_refused line and takes no pass, with or without a valid overlay and a facts file a pass
would record. The launchd unit's own options ``--omni-home`` and ``--state-dir`` stay accepted
and the pass they select still runs: with the facts file missing it ends in facts_unread with a
status line on stdout. An unknown option refuses with argparse's exit 2 and records nothing, and
a really run pass without an overlay still refuses with overlay_refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import main
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    overlay,
    scenario_facts,
)

pytestmark = pytest.mark.unit

OWNER = "acme"
NODE = "node_pr_landing_snapshot_effect"


def _watcher_state() -> dict[str, object]:
    """The PR watcher's state for the facts' tick, fresh enough for a pass to take it."""
    prs: dict[str, object] = {}
    for i, pr in enumerate(scenario_facts(FIRST).prs):
        short = pr.pr.partition("/")[2]
        number = short.partition("#")[2]
        red = (
            [
                ["Tests (Split 1/2)", "failure", f"{number}0", "2026-11-10T02:00:00Z"],
                ["lint", "failure", f"{number}1", "2026-11-10T02:00:00Z"],
            ]
            if i
            else []
        )
        prs[short] = {
            "facts": {
                "head_sha": pr.head_sha,
                "node_id": f"PR_kwDOn{number}",
                "repo": short.partition("#")[0],
            },
            "ci": {"sha": pr.head_sha, "red_facts": red},
        }
    return {
        "last_tick": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prs": prs,
    }


def _write_overlay(tmp: Path, facts: Path) -> Path:
    """A valid redpanda overlay (bus_lane form) under tmp's overlay root, paths under tmp."""
    root = tmp / "overlay-roots"
    node = root / NODE
    node.mkdir(parents=True)
    base = overlay().model_dump()
    doc = {
        "backend": "redpanda",
        "record_topic": base["record_topic"],
        "failed_topic": base["failed_topic"],
        "floor_piece": base["floor_piece"],
        "bus_lane": "dev",
        "workspace_root": str(tmp),
        "facts_path": str(facts),
        "watcher_state_path": str(tmp / "state.json"),
        "watcher_max_age_seconds": 900,
        "owner": OWNER,
        "record_journal": str(tmp / "records.jsonl"),
        "emission_journal": str(tmp / "emissions.jsonl"),
    }
    (node / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root


def _ready_backend(tmp: Path, facts: Path) -> Path:
    """The overlay plus the facts file and watcher state a real pass would take."""
    root = _write_overlay(tmp, facts)
    (tmp / "last-facts.json").write_text(
        scenario_facts(FIRST).model_dump_json(), encoding="utf-8"
    )
    (tmp / "state.json").write_text(json.dumps(_watcher_state()), encoding="utf-8")
    return root


def test_help_exits_zero_with_the_usage_and_no_overlay_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "overlay_refused" not in out


def test_help_records_nothing_even_with_a_valid_overlay_and_a_recordable_facts_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _ready_backend(tmp_path, tmp_path / "last-facts.json")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    assert not (tmp_path / "records.jsonl").exists()
    assert not (tmp_path / "emissions.jsonl").exists()


def test_the_units_own_arguments_still_take_a_pass_that_ends_in_facts_unread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    facts = (
        tmp_path / "missing-facts.json"
    )  # never written: the pass ends facts_unread, no bus
    root = _ready_backend(tmp_path, facts)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    code = main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 3
    assert json.loads(out)["status"] == "facts_unread"


def test_an_unknown_option_refuses_with_argparse_exit_two_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _ready_backend(tmp_path, tmp_path / "last-facts.json")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    with pytest.raises(SystemExit) as raised:
        main(["--no-such"])
    assert raised.value.code == 2
    assert not (tmp_path / "records.jsonl").exists()
    assert not (tmp_path / "emissions.jsonl").exists()


def test_a_really_run_pass_without_an_overlay_still_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    code = main([])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "overlay_refused"
