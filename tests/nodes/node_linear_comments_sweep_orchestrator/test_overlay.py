# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The private half of the sweep arrives only through a strict overlay."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_linear_comments_sweep_orchestrator.handlers.handler_linear_comments_sweep import (
    OVERLAY_ENV,
    HandlerLinearCommentsSweep,
    LinearCommentsSweepConfigurationError,
    load_sweep_overlay,
)

from .helpers import (
    NODE,
    OVERLAY_FILE,
    OVERLAY_ROOT,
    FixtureGateway,
    RealDecisions,
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
    with pytest.raises(LinearCommentsSweepConfigurationError, match=OVERLAY_ENV):
        load_sweep_overlay()


def test_pointer_wins_over_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(
        other / "overlay.yaml",
        {"values": {"report_dir": "x", "metrics_path": "y", "rules": "z"}},
    )
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(other.parent))
    assert load_sweep_overlay().values["report_dir"] == "drafts"


def test_roots_use_the_first_existing_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _copy_overlay(tmp_path)
    _rewrite(
        other / "overlay.yaml",
        {"values": {"report_dir": "x", "metrics_path": "y", "rules": "z"}},
    )
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join(
            ("", str(tmp_path / "missing"), str(other.parent), str(OVERLAY_ROOT))
        ),
    )
    assert load_sweep_overlay().values["report_dir"] == "x"


@pytest.mark.parametrize("pointer", ["", "/nonexistent/overlay.yaml"])
def test_an_explicit_bad_pointer_never_falls_through(
    pointer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, pointer)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    with pytest.raises(LinearCommentsSweepConfigurationError, match=OVERLAY_ENV):
        load_sweep_overlay()


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"unexpected": 1}, "unknown"),
        ({"values": {"report_dir": "x"}}, "missing values"),
        ({"values": "none"}, "mapping"),
        (
            {"values": {"report_dir": "x", "metrics_path": "y", "rules": " "}},
            "nonempty",
        ),
        (
            {
                "values": {
                    "report_dir": "x",
                    "metrics_path": "y",
                    "rules": "z",
                    "q": "q",
                }
            },
            "unknown values",
        ),
        ({"briefs": "absent.md"}, "briefs"),
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
    with pytest.raises(LinearCommentsSweepConfigurationError, match=fragment):
        load_sweep_overlay()


@pytest.mark.parametrize(
    "label",
    ["linear-comments-collect", "linear-comments-verify", "linear-comments-write"],
)
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
    with pytest.raises(LinearCommentsSweepConfigurationError, match=label):
        load_sweep_overlay()


def test_rendered_prompts_carry_the_overlay_values_and_the_window() -> None:
    gateway = FixtureGateway()
    asyncio.run(
        HandlerLinearCommentsSweep(
            None, gateway, RealDecisions(), public_overlay()
        ).handle(request())
    )
    prompts = {c.label.split(":")[0]: c.prompt for c in gateway.calls}
    assert "since 2026-09-14T10:40:31Z" in prompts["collect"]
    assert "READ-ONLY on the tracker" in prompts["collect"]
    assert "FACT-1" in prompts["verify"]
    assert "OMN-18268" in prompts["verify"] or "OMN-" in prompts["verify"]
    assert "metrics/linear-comments.jsonl" in prompts["write"]
    assert "drafts/linear-comment-replies-2026-09-14T104031Z.md" in prompts["write"]
    assert "@@" not in "".join(c.prompt for c in gateway.calls)


def test_the_node_package_holds_no_private_identity() -> None:
    """The public node names no private repository, workspace, lane or path."""
    forbidden = (
        "-".join(("knowledge", "base", "internal")),
        "_".join(("omni", "home")),
        "_".join(("omnibase", "internal")),
        "_".join(("OMNI", "HOME")),
        "/".join(("beta", "tracking")),
        "_".join(("ROLLING", "WORK", "LEDGER")),
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
    HandlerLinearCommentsSweep(None, FixtureGateway(), RealDecisions())
    HandlerLinearCommentsSweep(None)


def test_a_run_without_an_overlay_is_refused_before_any_phase_runs() -> None:
    gateway = FixtureGateway()
    decisions = RealDecisions()
    handler = HandlerLinearCommentsSweep(None, gateway, decisions)
    with pytest.raises(LinearCommentsSweepConfigurationError, match=OVERLAY_ENV):
        asyncio.run(handler.handle(request()))
    assert gateway.calls == []
    assert decisions.kinds == []


def test_a_wired_handler_picks_the_overlay_up_once_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FixtureGateway()
    handler = HandlerLinearCommentsSweep(None, gateway, RealDecisions())
    with pytest.raises(LinearCommentsSweepConfigurationError):
        asyncio.run(handler.handle(request()))
    monkeypatch.setenv(OVERLAY_ENV, str(OVERLAY_FILE))
    asyncio.run(handler.handle(request()))
    assert gateway.calls
