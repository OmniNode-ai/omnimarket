# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The private half of the morning process arrives only through a strict overlay."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    OVERLAY_ENV,
    HandlerMorningGroundState,
    MorningGroundStateConfigurationError,
    load_morning_overlay,
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
    with pytest.raises(MorningGroundStateConfigurationError, match=OVERLAY_ENV):
        load_morning_overlay()


def test_pointer_wins_over_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(
        other / "overlay.yaml",
        {"request_defaults": {**_defaults(), "closure_probe_ticket": "OTHER-1"}},
    )
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(other.parent))
    assert load_morning_overlay().request_defaults.closure_probe_ticket == "PROBE-1"


def test_roots_use_the_first_existing_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(
        other / "overlay.yaml",
        {"request_defaults": {**_defaults(), "closure_probe_ticket": "OTHER-1"}},
    )
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join(
            ("", str(tmp_path / "missing"), str(other.parent), str(OVERLAY_ROOT))
        ),
    )
    assert load_morning_overlay().request_defaults.closure_probe_ticket == "OTHER-1"


@pytest.mark.parametrize("pointer", ["", "/nonexistent/overlay.yaml"])
def test_an_explicit_bad_pointer_never_falls_through(
    pointer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, pointer)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    with pytest.raises(MorningGroundStateConfigurationError, match=OVERLAY_ENV):
        load_morning_overlay()


def _defaults() -> dict[str, object]:
    raw = yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
    defaults: dict[str, object] = raw["request_defaults"]
    return defaults


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"unexpected": 1}, "unknown"),
        ({"values": {"LEDGER_PATH": "x"}}, "missing value"),
        ({"texts": {"dry": "x"}}, "missing text"),
        ({"briefs": "absent.md"}, "briefs"),
        ({"request_defaults": {"plan_docs": "not-a-list"}}, "request_defaults"),
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
    with pytest.raises(MorningGroundStateConfigurationError, match=fragment):
        load_morning_overlay()


def test_an_overlay_missing_a_required_template_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied = _copy_overlay(tmp_path)
    briefs = copied / "briefs.md"
    briefs.write_text(
        re.sub(
            r"<!-- prompt:dropped-work -->.*?<!-- end:dropped-work -->\n",
            "",
            briefs.read_text(encoding="utf-8"),
            flags=re.S,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(OVERLAY_ENV, str(copied / "overlay.yaml"))
    with pytest.raises(MorningGroundStateConfigurationError, match="dropped-work"):
        load_morning_overlay()


def test_rendered_prompts_carry_the_overlay_values() -> None:
    gateway = FixtureGateway(
        {
            "phases": [
                {
                    "phase": "Triage",
                    "verdict": "already-delivered",
                    "evidence": "abc123",
                    "reason": "fixture",
                }
            ]
        }
    )
    asyncio.run(
        HandlerMorningGroundState(None, gateway, public_overlay()).handle(
            request({"publish": False})
        )
    )
    ground = gateway.prompts["ground-state"]
    assert "reports/2026-08-30-ground-state.md" in ground
    assert "PROBE-1" in ground
    assert "repo_a, repo_b" in ground
    assert "plans/example-plan.md" in ground
    assert "automation/example-2026-08-30" in ground
    assert "DRY mode." in gateway.prompts["session-goal"]
    assert (
        "tracking/LEDGER.md (this date's triage row)"
        in gateway.prompts["plan-reconcile"]
    )
    assert "@@" not in "".join(gateway.prompts.values())


def test_request_values_override_the_overlay_defaults() -> None:
    gateway = FixtureGateway()
    asyncio.run(
        HandlerMorningGroundState(None, gateway, public_overlay()).handle(
            request(
                {
                    "force": True,
                    "closureProbeTicket": "MINE-9",
                    "integrationRepos": ["only_repo"],
                    "planDocs": [],
                    "kbInternalPath": "/fixture/clone",
                }
            )
        )
    )
    ground = gateway.prompts["ground-state"]
    assert "MINE-9" in ground
    assert "PROBE-1" not in ground
    assert "only_repo" in ground
    assert "repo_a" not in ground
    assert "/fixture/clone" in ground
    assert "plans/example-plan.md" not in ground


def test_the_node_package_holds_no_private_identity() -> None:
    """The public node names no private repository, workspace or path."""
    forbidden = (
        "knowledge-base-internal",
        "omni_home",
        "omnibase_internal",
        "OMNI_HOME",
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


def test_wiring_the_handler_without_an_overlay_does_not_raise() -> None:
    """OMN-17427: a runtime without this node's overlay must still boot.

    The effects runtime builds every handler at auto-wiring, and one that raises
    takes the whole process down with it. The overlay is a deployment fact of a
    RUN, so its absence is refused when a run starts, never when the runtime does.
    """
    HandlerMorningGroundState(None, FixtureGateway())


def test_a_run_without_an_overlay_is_refused_before_any_phase_runs() -> None:
    gateway = FixtureGateway()
    handler = HandlerMorningGroundState(None, gateway)
    with pytest.raises(MorningGroundStateConfigurationError, match=OVERLAY_ENV):
        asyncio.run(handler.handle(request({"force": True})))
    assert gateway.calls == []
    assert gateway.reconciled == []


def test_a_wired_handler_picks_the_overlay_up_once_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FixtureGateway()
    handler = HandlerMorningGroundState(None, gateway)
    with pytest.raises(MorningGroundStateConfigurationError):
        asyncio.run(handler.handle(request({"force": True})))
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    asyncio.run(handler.handle(request({"force": True})))
    assert gateway.calls
