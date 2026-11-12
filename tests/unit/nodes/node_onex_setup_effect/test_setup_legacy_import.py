# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Old shell selections are inert hints; phase files never prove live effects.

Defined before the importer: malformed/ambiguous records, shell execution,
symlink targets, and repeated import must all be falsifiable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_legacy_import import (
    HandlerSetupLegacyImport,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from tests.unit.nodes.node_onex_setup_effect.test_setup_workflow import MarkerStep

pytestmark = pytest.mark.unit


def test_nonexistent_legacy_directory_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert HandlerSetupLegacyImport().handle(missing) is None
    assert not missing.exists()


def test_selections_imported_as_hints_without_changing_sources(tmp_path: Path) -> None:
    (tmp_path / "choices").write_text(
        "models=gemini local\ncontainers=0\n"
        "local_url=http://127.0.0.1:18001/v1\nlocal_model=foo\n"
    )
    (tmp_path / "phases.v2.done").write_text("1\n2\n")
    (tmp_path / "stack.anchor").write_text("a" * 40 + "\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = HandlerSetupLegacyImport().handle(tmp_path)
    assert result is not None
    assert result.providers == ("gemini", "local")
    assert result.containers is False
    assert result.local_url == "http://127.0.0.1:18001/v1"
    assert result.local_model == "foo"
    assert result.completed_phases == (1, 2)
    assert result.infra_anchor == "a" * 40
    assert not hasattr(result, "status")
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("choices", "models=gemini\nmodels=local\n"),
        ("choices", "api_key=SECRET_SENTINEL_71\n"),
        ("choices", "models=invalid_provider\n"),
        ("choices", "containers=yes\n"),
        ("choices", "local_url=http://user:SECRET_SENTINEL_71@localhost\n"),
        ("choices", "local_url=http://localhost/v1?api_key=SECRET_SENTINEL_71\n"),
        ("phases.v2.done", "7\n"),
        ("stack.anchor", "g" * 40),
    ],
)
def test_invalid_records_refused_without_echoing_input(
    tmp_path: Path, filename: str, content: str
) -> None:
    target = tmp_path / filename
    target.write_text(content)
    with pytest.raises(ValueError, match="legacy") as caught:
        HandlerSetupLegacyImport().handle(tmp_path)
    assert "SECRET_SENTINEL_71" not in str(caught.value)
    assert target.read_text() == content


def test_shell_literal_stays_data(tmp_path: Path) -> None:
    sentinel = tmp_path / "must not be created"
    literal = f"$(touch '{sentinel}')"
    (tmp_path / "choices").write_text(f"local_model={literal}\n")
    result = HandlerSetupLegacyImport().handle(tmp_path)
    assert result is not None
    assert result.local_model == literal
    assert not sentinel.exists()


@pytest.mark.parametrize("filename", ["choices", "phases.v2.done", "stack.anchor"])
def test_symlink_sources_refused(tmp_path: Path, filename: str) -> None:
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign content")
    (tmp_path / filename).symlink_to(foreign)
    with pytest.raises(ValueError, match="legacy"):
        HandlerSetupLegacyImport().handle(tmp_path)
    assert foreign.read_text() == "foreign content"


def test_empty_record_is_still_a_migration_without_effect_proof(tmp_path: Path) -> None:
    (tmp_path / "phases.v2.done").touch()
    result = HandlerSetupLegacyImport().handle(tmp_path)
    assert result is not None
    assert result.providers == ()
    assert result.completed_phases == ()


def test_shell_import_is_once_and_phase_marks_cannot_skip_effect(
    tmp_path: Path,
) -> None:
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "choices").write_text("models=local\ncontainers=0\n")
    (old / "phases.v2.done").write_text("1\n2\n3\n4\n5\n6\n")
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex",
        steps=(step,),
        legacy_onboarding=old,
        read_legacy=HandlerSetupLegacyImport().handle,
    )
    plan = ModelSetupPlan(workspace=tmp_path / "workspace")
    first = engine.handle(plan)
    assert first.imported_legacy
    assert first.legacy_hints is not None
    assert first.legacy_hints.completed_phases == (1, 2, 3, 4, 5, 6)
    assert step.applied == 1
    (old / "choices").write_text("api_key=SECRET_SENTINEL_71\n")
    second = engine.handle(plan)
    assert not second.imported_legacy
    assert second.legacy_hints == first.legacy_hints
    assert step.applied == 1


def test_verify_does_not_commit_migration(tmp_path: Path) -> None:
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "choices").write_text("models=local\n")
    home = tmp_path / "onex"
    step = MarkerStep("workspace", tmp_path / "effects")
    result = HandlerSetupWorkflow(
        onex_home=home,
        steps=(step,),
        legacy_onboarding=old,
        read_legacy=HandlerSetupLegacyImport().handle,
    ).handle(ModelSetupPlan(workspace=tmp_path / "workspace", action="verify"))
    assert not result.imported_legacy
    assert not home.exists()
    assert step.applied == 0


def test_journal_keeps_selected_configuration(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    plan = ModelSetupPlan(
        workspace=tmp_path / "workspace",
        components=("delegation", "dashboard"),
        providers=(
            {
                "provider": "local",
                "models": ("m1", "m2"),
                "url": "http://127.0.0.1:18001",
            },
        ),
    )
    journal = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan
    )
    assert journal.plan == plan
    assert journal.plan.providers[0].models == ("m1", "m2")
