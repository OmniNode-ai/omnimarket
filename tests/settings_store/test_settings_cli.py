# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex settings set|get|list|bootstrap-file`: one declared command over the central settings store (Task 21)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.handlers.overlay_store.bootstrap_file import (
    BOOTSTRAP_BINDINGS,
    parse_bootstrap_file,
)
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.handlers.settings import settings_cli
from omnimarket.handlers.settings.settings_cli import settings_group

NOW = datetime(2026, 11, 26, 21, 0, tzinfo=UTC).isoformat()


def _store_with_responders() -> InMemoryOverlayStore:
    store = InMemoryOverlayStore()
    asyncio.run(
        store.write_version(
            {
                "overlay_id": "delegation_check_responders",
                "digest": "d1",
                "revision": 3,
                "loaded_at": NOW,
                "document": {"schema_version": 1, "responders": ["a", "b"]},
            }
        )
    )
    return store


def _run(args: list[str], store: InMemoryOverlayStore | None = None) -> object:
    return CliRunner().invoke(
        settings_group, args, obj={"store": store or _store_with_responders()}
    )


def test_get_prints_the_stored_value_as_json() -> None:
    result = _run(["get", "delegation_check_responders", "responders"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["a", "b"]


def test_get_refuses_an_undeclared_key_and_prints_nothing() -> None:
    result = _run(["get", "delegation_check_responders", "not_a_key"])
    assert result.exit_code != 0
    assert "not_a_key" in result.output
    assert "[" not in result.output


def test_get_of_an_overlay_the_store_lacks_exits_nonzero() -> None:
    result = _run(["get", "goal_scope_ticket_rule", "ticket_roots"])
    assert result.exit_code != 0
    assert "goal_scope_ticket_rule" in result.output


def test_list_names_each_declared_setting_with_its_current_value() -> None:
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(
        line.startswith("delegation_check_responders\tresponders\t")
        and '["a", "b"]' in line
        for line in lines
    )
    # a declared setting with no stored record is listed as unset, never dropped
    assert any(
        line.startswith("goal_scope_ticket_rule\tticket_roots\t")
        and line.endswith("<unset>")
        for line in lines
    )


def test_list_can_be_narrowed_to_one_overlay() -> None:
    result = _run(["list", "--overlay", "delegation_check_responders"])
    assert result.exit_code == 0, result.output
    assert all(
        line.startswith("delegation_check_responders\t")
        for line in result.output.splitlines()
    )


def test_set_hands_the_declared_setter_its_arguments_and_passes_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(settings_cli, "_set_main", fake_main)
    result = _run(
        [
            "set",
            "delegation_check_responders",
            "responders",
            '["x"]',
            "--actor",
            "lane-1",
        ]
    )
    assert result.exit_code == 0, result.output
    assert seen == [
        [
            "--overlay",
            "delegation_check_responders",
            "--key",
            "responders",
            "--actor",
            "lane-1",
            "--value-json",
            '["x"]',
        ]
    ]


def test_set_refuses_an_undeclared_key_without_calling_the_setter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_cli, "_set_main", lambda _argv: pytest.fail("the setter was called")
    )
    result = _run(["set", "delegation_check_responders", "nope", "1", "--actor", "x"])
    assert result.exit_code != 0
    assert "nope" in result.output


def test_set_refuses_a_value_that_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_cli, "_set_main", lambda _argv: pytest.fail("the setter was called")
    )
    result = _run(
        ["set", "delegation_check_responders", "responders", "{bad", "--actor", "x"]
    )
    assert result.exit_code != 0


def test_set_passes_a_failing_setter_exit_code_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_cli, "_set_main", lambda _argv: 65)
    result = _run(
        ["set", "delegation_check_responders", "responders", "[]", "--actor", "x"]
    )
    assert result.exit_code == 65


def test_bootstrap_file_renders_the_bindings_only(tmp_path: Path) -> None:
    intake = tmp_path / "intake.env"
    intake.write_text(
        "".join(f"{name}=v-{name}\n" for name in BOOTSTRAP_BINDINGS)
        + "SOME_SETTING=must-not-travel\n",
        encoding="utf-8",
    )
    out = tmp_path / "bootstrap.env"
    result = CliRunner().invoke(
        settings_group, ["bootstrap-file", "--intake", str(intake), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert parse_bootstrap_file(out.read_text(encoding="utf-8")) == {
        name: f"v-{name}" for name in BOOTSTRAP_BINDINGS
    }
    assert "SOME_SETTING" not in out.read_text(encoding="utf-8")


def test_bootstrap_file_refuses_by_name_when_an_input_is_missing(
    tmp_path: Path,
) -> None:
    intake = tmp_path / "intake.env"
    names = list(BOOTSTRAP_BINDINGS)
    missing = names.pop()
    intake.write_text("".join(f"{n}=v\n" for n in names), encoding="utf-8")
    out = tmp_path / "bootstrap.env"
    result = CliRunner().invoke(
        settings_group, ["bootstrap-file", "--intake", str(intake), "--out", str(out)]
    )
    assert result.exit_code != 0
    assert missing in result.output
    assert not out.exists()


def _overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "roots"
    home = root / "delegation_check_responders"
    home.mkdir(parents=True)
    (home / "overlay.yaml").write_text(
        "schema_version: 1\nresponders: [old]\n", encoding="utf-8"
    )
    return root


def test_a_successful_set_rewrites_the_overlays_local_file_with_value_and_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import yaml

    root = _overlay_root(tmp_path)
    monkeypatch.setattr(settings_cli, "_set_main", lambda _argv: 0)
    result = CliRunner().invoke(
        settings_group,
        [
            "set",
            "delegation_check_responders",
            "responders",
            '["new"]',
            "--actor",
            "lane-7",
        ],
        obj={"store": _store_with_responders()},
        env={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert result.exit_code == 0, result.output
    text = (root / "delegation_check_responders" / "overlay.yaml").read_text(
        encoding="utf-8"
    )
    assert yaml.safe_load(text) == {"schema_version": 1, "responders": ["new"]}
    provenance = [
        line for line in text.splitlines() if line.startswith("# settings-set:")
    ]
    assert len(provenance) == 1
    assert "responders" in provenance[0]
    assert "lane-7" in provenance[0]
    assert "new" not in provenance[0].replace(
        "settings-set", ""
    )  # the value never rides in the comment


def test_a_second_set_keeps_the_earlier_provenance_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _overlay_root(tmp_path)
    monkeypatch.setattr(settings_cli, "_set_main", lambda _argv: 0)
    for actor, value in (("lane-1", '["a"]'), ("lane-2", '["b"]')):
        result = CliRunner().invoke(
            settings_group,
            [
                "set",
                "delegation_check_responders",
                "responders",
                value,
                "--actor",
                actor,
            ],
            obj={"store": _store_with_responders()},
            env={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
        )
        assert result.exit_code == 0, result.output
    text = (root / "delegation_check_responders" / "overlay.yaml").read_text(
        encoding="utf-8"
    )
    lines = [line for line in text.splitlines() if line.startswith("# settings-set:")]
    assert len(lines) == 2
    assert "lane-1" in lines[0]
    assert "lane-2" in lines[1]


def test_a_failed_set_leaves_the_local_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _overlay_root(tmp_path)
    path = root / "delegation_check_responders" / "overlay.yaml"
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(settings_cli, "_set_main", lambda _argv: 65)
    result = CliRunner().invoke(
        settings_group,
        ["set", "delegation_check_responders", "responders", '["new"]', "--actor", "x"],
        obj={"store": _store_with_responders()},
        env={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    assert result.exit_code == 65
    assert path.read_text(encoding="utf-8") == before


def test_a_set_with_no_local_overlay_file_succeeds_and_says_nothing_was_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings_cli, "_set_main", lambda _argv: 0)
    result = CliRunner().invoke(
        settings_group,
        ["set", "delegation_check_responders", "responders", '["new"]', "--actor", "x"],
        obj={"store": _store_with_responders()},
        env={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "empty")},
    )
    assert result.exit_code == 0, result.output
    assert "no local overlay file" in result.output
