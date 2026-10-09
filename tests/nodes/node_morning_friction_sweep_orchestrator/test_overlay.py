# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The private half of the friction sweep arrives only through a strict overlay."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_morning_friction_sweep import (
    OVERLAY_ENV,
    HandlerMorningFrictionSweep,
    MorningFrictionSweepConfigurationError,
    load_friction_overlay,
)

from .helpers import (
    NODE,
    OVERLAY_FILE,
    OVERLAY_ROOT,
    FixtureGateway,
    public_overlay,
    request,
)

PACKAGE = f"omnimarket.nodes.{NODE}"


@pytest.fixture(autouse=True)
def clear_overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


def _copy_overlay(tmp_path: Path) -> Path:
    target = tmp_path / "copy" / NODE
    shutil.copytree(OVERLAY_ROOT / NODE, target)
    return target


def _rewrite(overlay: Path, change: dict[str, object]) -> None:
    raw = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    raw.update(change)
    overlay.write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_no_overlay_is_a_hard_stop() -> None:
    with pytest.raises(MorningFrictionSweepConfigurationError, match=OVERLAY_ENV):
        load_friction_overlay()


def test_pointer_wins_over_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(other / "overlay.yaml", {"ci_repos": ["other_repo"]})
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(other.parent))
    assert load_friction_overlay().lists["ci_repos"] == ["repo_a", "repo_b"]


def test_roots_use_the_first_existing_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(other / "overlay.yaml", {"ci_repos": ["other_repo"]})
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join(
            ("", str(tmp_path / "missing"), str(other.parent), str(OVERLAY_ROOT))
        ),
    )
    assert load_friction_overlay().lists["ci_repos"] == ["other_repo"]


@pytest.mark.parametrize("pointer", ["", "/nonexistent/overlay.yaml"])
def test_an_explicit_bad_pointer_never_falls_through(
    pointer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, pointer)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    with pytest.raises(MorningFrictionSweepConfigurationError, match=OVERLAY_ENV):
        load_friction_overlay()


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"unexpected": 1}, "unknown"),
        ({"values": {"state_path": "x"}}, "missing value"),
        ({"texts": {"report_dry": "x"}}, "missing text"),
        ({"briefs": "absent.md"}, "briefs"),
        ({"ci_repos": "not-a-list"}, "ci_repos"),
        ({"premise_control_lanes": []}, "premise_control_lanes"),
    ],
)
def test_an_invalid_overlay_is_refused(
    change: dict[str, object],
    fragment: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = _copy_overlay(tmp_path)
    _rewrite(copied / "overlay.yaml", change)
    monkeypatch.setenv(OVERLAY_ENV, str(copied / "overlay.yaml"))
    with pytest.raises(MorningFrictionSweepConfigurationError, match=fragment):
        load_friction_overlay()


@pytest.mark.parametrize("label", ["friction-report", "friction-source-ci"])
def test_an_overlay_missing_a_required_template_is_refused(
    label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied = _copy_overlay(tmp_path)
    briefs = copied / "briefs.md"
    briefs.write_text(
        re.sub(
            rf"<!-- prompt:{label} -->.*?<!-- end:{label} -->\n",
            "",
            briefs.read_text(encoding="utf-8"),
            flags=re.S,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(OVERLAY_ENV, str(copied / "overlay.yaml"))
    with pytest.raises(MorningFrictionSweepConfigurationError, match=label):
        load_friction_overlay()


def test_rendered_prompts_carry_the_overlay_values() -> None:
    gateway = FixtureGateway()
    asyncio.run(
        HandlerMorningFrictionSweep(None, gateway, public_overlay()).handle(
            request({"dryRun": True, "lookbackHours": 72, "fences": ["held: x"]})
        )
    )
    scan = gateway.prompts["friction-scan"]
    assert "reports/friction-state.json" in scan
    assert "lookback 72" in scan
    assert "- held: x" in scan
    assert (
        "window [2026-08-28T00:00:00Z, 2026-08-29T00:00:00Z) — the previous UTC day, 2026-08-28"
        in scan
    )
    assert "repo_a, repo_b" in gateway.prompts["friction-source-ci"]
    assert "'stem-scan', 'stem-report'" in gateway.prompts["friction-precheck"]
    assert "DRY RUN, no ticket mutation." in gateway.prompts["friction-adjudicate"]
    assert "dry_run=true" in gateway.prompts["friction-adjudicate"]
    assert "DRY report." in gateway.prompts["friction-report"]
    assert "automation/friction-2026-08-29" in gateway.prompts["friction-report"]
    assert "reports/2026-08-29-friction.md" in gateway.prompts["friction-report"]
    assert "/tmp/synthesis.json" in gateway.prompts["friction-adjudicate"]
    assert "@@" not in "".join(gateway.prompts.values())


def test_a_live_run_names_the_live_modes() -> None:
    gateway = FixtureGateway()
    asyncio.run(
        HandlerMorningFrictionSweep(None, gateway, public_overlay()).handle(
            request({"force": True})
        )
    )
    assert "LIVE, ticket mutation authorized." in gateway.prompts["friction-adjudicate"]
    assert "LIVE report." in gateway.prompts["friction-report"]
    assert "FENCES: none passed for this run." in gateway.prompts["friction-scan"]


def test_the_report_schema_names_the_overlay_control_lanes() -> None:
    gateway = FixtureGateway()
    asyncio.run(
        HandlerMorningFrictionSweep(None, gateway, public_overlay()).handle(
            request({"force": True})
        )
    )
    report = next(c for c in gateway.calls if c["label"] == "friction-report")
    schema = str(report["schema"])
    assert "control-lane-a" in schema
    assert "control-lane-c" in schema


def test_the_node_package_holds_no_private_identity() -> None:
    """The public node names no private repository, workspace, lane or path."""
    forbidden = (
        "knowledge-base-internal",
        "omni_home",
        "omnibase_internal",
        "OMNI_HOME",
        "beta/tracking",
        "ROLLING_WORK_LEDGER",
        "omn16831",
    )
    root = Path(str(files(PACKAGE)))
    offenders = [
        f"{path.relative_to(root)}: {word}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".yaml", ".json", ".md"}
        for word in forbidden
        if word in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
