# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-22902): an allowlist entry for a not-yet-wired node expires on its date and reads as a finding.

An entry excuses the unreachable modules it covers while it holds. From its ``expires`` date it
excuses nothing and is itself a finding, whether or not the head adds anything. The failing control
is the entry's own date: the same head that passes the day before reads red on the day.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    check_added,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelWiringAllowance
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    REF,
    commit,
    make_clone,
    overlay,
)
from tests.unit.nodes.node_code_graph_effect.pr_support import (
    NOT_YET_WIRED,
    memory_source,
)

pytestmark = pytest.mark.unit

EXPIRES = date(2026, 12, 1)
ENTRY = ModelWiringAllowance(
    repo="example_app",
    path="src/example_app/not_yet_wired",
    reason="the node lands next week",
    expires=EXPIRES,
)
HANDLER = "src/example_app/not_yet_wired/node_b/handler.py"
SHA = "3" * 40


def _check(today: date, files: dict[str, str] | None = None):
    source = memory_source({SHA: NOT_YET_WIRED if files is None else files})
    rules = GraphRules(roots=overlay().graph_roots)
    base = source.snapshot("example_app", REF, rules.wanted)
    head = source.snapshot("example_app", SHA, rules.wanted)
    return check_added(base, head, rules, allowlist=(ENTRY,), today=today)


def test_a_live_entry_excuses_the_module_it_covers() -> None:
    result = _check(date(2026, 11, 7))
    assert result.passed
    assert result.allowed == (HANDLER,)
    assert result.flagged == ()
    assert result.expired == ()


def test_the_entry_still_holds_on_its_last_day() -> None:
    assert _check(date(2026, 11, 30)).passed


def test_the_entry_reads_as_a_finding_on_its_date_and_excuses_nothing() -> None:
    result = _check(EXPIRES)
    assert not result.passed
    assert result.expired == ("src/example_app/not_yet_wired expired 2026-12-01",)
    assert result.flagged == (HANDLER,)
    assert result.allowed == ()


def test_an_expired_entry_is_a_finding_even_when_the_head_adds_nothing() -> None:
    result = _check(date(2027, 1, 1), files={})
    assert result.added == 0
    assert result.flagged == ()
    assert not result.passed
    assert len(result.expired) == 1


def test_failing_control_the_same_head_flips_on_the_date() -> None:
    assert _check(date(2026, 11, 30)).passed
    assert not _check(EXPIRES).passed


def test_an_entry_of_another_repository_is_not_judged() -> None:
    source = memory_source({SHA: NOT_YET_WIRED})
    rules = GraphRules(roots=overlay().graph_roots)
    base = source.snapshot("example_app", REF, rules.wanted)
    head = source.snapshot("example_app", SHA, rules.wanted)
    other = ENTRY.model_copy(update={"repo": "example_core"})
    result = check_added(base, head, rules, allowlist=(other,), today=EXPIRES)
    assert result.expired == ()
    assert result.flagged == (HANDLER,)


def test_an_allowlist_without_a_day_is_refused() -> None:
    source = memory_source({SHA: NOT_YET_WIRED})
    rules = GraphRules(roots=overlay().graph_roots)
    base = source.snapshot("example_app", REF, rules.wanted)
    head = source.snapshot("example_app", SHA, rules.wanted)
    with pytest.raises(ValueError, match="today"):
        check_added(base, head, rules, allowlist=(ENTRY,))


def test_an_entry_names_a_directory_or_exactly_one_file() -> None:
    assert ENTRY.covers(HANDLER)
    assert not ENTRY.covers("src/example_app/not_yet_wired_other/x.py")
    exact = ENTRY.model_copy(update={"path": HANDLER})
    assert exact.covers(HANDLER)
    assert not exact.covers("src/example_app/not_yet_wired/node_b/other.py")


def test_the_overlay_refuses_an_entry_without_a_date_or_for_an_undeclared_repository() -> (
    None
):
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["allowlist"][0]["expires"]
    with pytest.raises(ValidationError):
        overlay(allowlist=document["allowlist"])
    with pytest.raises(ValidationError, match="undeclared repository"):
        overlay(
            allowlist=[
                {"repo": "nowhere", "path": "x", "reason": "r", "expires": "2026-12-01"}
            ]
        )


def test_the_check_names_the_expired_entry_through_the_node_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    document["allowlist"] = [
        {
            "repo": "example_app",
            "path": "src/example_app/not_yet_wired",
            "reason": "the node lands next week",
            "expires": "2020-01-01",
        }
    ]
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    head = commit(clones / "example_app", NOT_YET_WIRED, "not yet wired")

    code = main(["check", "--repo", "example_app", "--base", REF, "--head", head])
    out = capsys.readouterr().out
    assert code == 1, out
    assert (
        "ALLOWLIST-EXPIRED example_app src/example_app/not_yet_wired expired 2020-01-01"
        in out
    )
    assert f"UNREACHABLE-ADDED example_app {HANDLER}" in out

    document["allowlist"][0]["expires"] = "2099-01-01"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    code = main(["check", "--repo", "example_app", "--base", REF, "--head", head])
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"ALLOWED example_app {HANDLER}" in out
