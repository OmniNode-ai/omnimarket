# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The de-banked dep-health findings are accounted for, and still count as new.

The shrink-only baseline commit cut ``.onex_state/dep_health_baseline.json`` from 584
entries to 49. ``.onex_state/dep_health_debanked.json`` lists the 535 removed entries,
each with an owner, so they are tracked rather than forgotten. The ledger is not an
allowlist: the sweep never reads it, and a finding listed there is still refused as new.
The delta step stays non-blocking only while the ledger still has entries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = ".onex_state/dep_health_baseline.json"
LEDGER = REPO_ROOT / ".onex_state" / "dep_health_debanked.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dep-health-gate.yml"
DEBANK_COMMIT = "ff67f869ebf5ce2c1b9c739f972c6da0c7057612"
_LEDGER_ONLY_FIELDS = ("owning_node", "owner")
_TOPIC_CLASSES = ("MISSING_TOPIC_EDGE", "UNDECLARED_TOPIC")


def _ledger() -> dict[str, Any]:
    if not LEDGER.is_file():
        pytest.fail(f"{LEDGER.relative_to(REPO_ROOT)} is missing")
    ledger: dict[str, Any] = json.loads(LEDGER.read_text(encoding="utf-8"))
    return ledger


def _baseline_at(rev: str) -> Counter[str]:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{rev}:{BASELINE}"],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"cannot read {rev}:{BASELINE} (needs full git history): {proc.stderr.strip()}"
        )
    findings = json.loads(proc.stdout)["findings"]
    return Counter(json.dumps(f, sort_keys=True) for f in findings)


def test_ledger_reconciles_entry_for_entry_with_the_debank_commit() -> None:
    """The ledger is exactly the multiset of rows the commit removed from the baseline."""
    ledger = _ledger()
    entries = ledger["entries"]
    listed = Counter(
        json.dumps(
            {k: v for k, v in e.items() if k not in _LEDGER_ONLY_FIELDS}, sort_keys=True
        )
        for e in entries
    )
    before = _baseline_at(f"{DEBANK_COMMIT}^")
    after = _baseline_at(DEBANK_COMMIT)

    assert not after - before, "the debank commit added baseline rows"
    assert listed == before - after
    assert ledger["source_commit"] == DEBANK_COMMIT
    assert ledger["removed_count"] == len(entries) == 535
    for field, summary in (
        ("finding_type", "by_finding_type"),
        ("severity", "by_severity"),
        ("owner", "by_owner"),
    ):
        assert ledger[summary] == dict(Counter(e[field] for e in entries))


def test_every_entry_names_an_owner_and_owning_node() -> None:
    """Each entry names a person and the package it lives in; each topic has one owner."""
    topic_owners: dict[str, set[str]] = {}
    for entry in _ledger()["entries"]:
        assert entry["owner"].strip(), entry
        assert entry["owner"] != "unowned", entry
        assert entry["file_path"].startswith(entry["owning_node"] + "/"), entry
        if entry["finding_type"] in _TOPIC_CLASSES:
            topic_owners.setdefault(entry["symbol"], set()).add(entry["owner"])
    assert topic_owners
    assert {t: o for t, o in topic_owners.items() if len(o) != 1} == {}


def test_delta_step_stays_non_blocking_while_ledger_has_live_entries() -> None:
    """Re-blocking while de-banked findings remain would fail every branch again."""
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["dep-health"][
        "steps"
    ]
    delta = [s for s in steps if "--delta-mode" in (s.get("run") or "")]
    assert len(delta) == 1, "expected exactly one --delta-mode step"
    if _ledger()["entries"]:
        assert delta[0].get("continue-on-error") is True


def test_a_debanked_finding_is_still_refused_as_new(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listing a finding in the ledger does not bank it: the delta sweep still exits 1."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    from run_dep_health_sweep import main

    topic = next(
        e["symbol"]
        for e in _ledger()["entries"]
        if e["finding_type"] == "MISSING_TOPIC_EDGE" and e["severity"] == "CRITICAL"
    )
    repo_root = tmp_path / "repo"
    contract_dir = (
        repo_root / "src" / "omnimarket" / "nodes" / "node_debanked_regression"
    )
    contract_dir.mkdir(parents=True)
    (contract_dir / "contract.yaml").write_text(
        f"""\
name: node_debanked_regression
node_type: EFFECT
event_bus:
  publish_topics:
    - "{topic}"
  subscribe_topics: []
""",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text((REPO_ROOT / BASELINE).read_text(encoding="utf-8"))

    rc = main(
        [
            "--repo-roots",
            str(repo_root),
            "--severity-threshold",
            "CRITICAL",
            "--baseline-path",
            str(baseline),
            "--delta-mode",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out)["new_findings_count"] >= 1
    assert "new finding(s)" in captured.err
