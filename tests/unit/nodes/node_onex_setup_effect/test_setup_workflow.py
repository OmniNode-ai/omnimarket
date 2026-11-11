# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure controls for setup reconciliation; these are not product E2E proof.

Failure modes: stale success, no-op provisioning, interrupted effects, concurrent
runs, unreadable state, and secrets in diagnostic/state surfaces. Each effect
below owns a real marker file so replay is judged by observed state, not a
recorded call count alone. Defined before the workflow implementation.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


class MarkerStep:
    """A recoverable file effect with optional faults, never a model/API stand-in."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.marker = root / name
        self.observed = 0
        self.applied = 0
        self.noop = False
        self.failure: str | None = None
        self.pending = False

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        self.observed += 1
        if self.pending:
            return ModelSetupObservation(
                step=self.name, status="action_required", fix="Complete sign-in."
            )
        expected = plan.model_dump_json()
        ready = self.marker.is_file() and self.marker.read_text() == expected
        return ModelSetupObservation(
            step=self.name, status="success" if ready else "unavailable"
        )

    def apply(self, plan: ModelSetupPlan) -> None:
        self.applied += 1
        if self.failure:
            raise RuntimeError(self.failure)
        if not self.noop:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(plan.model_dump_json())


def plan_at(tmp_path: Path, **kwargs: object) -> ModelSetupPlan:
    return ModelSetupPlan(workspace=tmp_path / "workspace with spaces", **kwargs)


def test_replay_reobserves_effect_without_duplicate_mutation(tmp_path: Path) -> None:
    plan = plan_at(tmp_path)
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    engine.handle(plan)
    before = step.observed
    result = engine.handle(plan)
    assert result.observations[-1].status == "success"
    assert step.observed > before
    assert step.applied == 1
    step.marker.unlink()
    engine.handle(plan)
    assert step.marker.is_file(), "saved success must not conceal a missing effect"
    assert step.applied == 2


def test_unimplemented_component_is_refused_before_state_or_machine_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
        HandlerSetupPreflight,
    )

    monkeypatch.setattr(
        HandlerSetupPreflight,
        "observe",
        lambda _self, _plan: ModelSetupObservation(
            step="preflight", status="action_required", fix="Isolated machine boundary."
        ),
    )
    home = tmp_path / "onex"
    engine = HandlerSetupWorkflow(onex_home=home)
    with pytest.raises(ValueError, match="docker"):
        engine.handle(plan_at(tmp_path, components=("docker",)))
    assert not home.exists()


def test_changed_provider_endpoint_reconciles_healthy_old_effect(
    tmp_path: Path,
) -> None:
    step = MarkerStep("models", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    first = engine.handle(
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:18001",
                },
            ),
        )
    )
    second = engine.handle(
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:18002",
                },
            ),
        )
    )
    assert first.desired_digest != second.desired_digest
    assert step.applied == 2
    assert "18002" in step.marker.read_text()


def test_verify_observes_without_creating_state_or_effects(tmp_path: Path) -> None:
    home = tmp_path / "absent"
    step = MarkerStep("workspace", tmp_path / "effects")
    result = HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(
        plan_at(tmp_path, action="verify")
    )
    assert result.observations[-1].status == "unavailable"
    assert step.observed == 1
    assert step.applied == 0
    assert not home.exists()
    assert not step.marker.parent.exists()


def test_noop_effect_never_becomes_success(tmp_path: Path) -> None:
    step = MarkerStep("models", tmp_path / "effects")
    step.noop = True
    later = MarkerStep("dashboard", tmp_path / "effects")
    result = HandlerSetupWorkflow(
        onex_home=tmp_path / "onex", steps=(step, later)
    ).handle(plan_at(tmp_path))
    assert result.observations[-1].status == "unavailable"
    assert step.applied == 1
    assert later.observed == 0


def test_interrupted_effect_resumes_without_replaying_proven_step(
    tmp_path: Path,
) -> None:
    home = tmp_path / "onex"
    early = MarkerStep("workspace", tmp_path / "effects")
    broken = MarkerStep("models", tmp_path / "effects")
    broken.failure = "SECRET_SENTINEL_71"
    later = MarkerStep("dashboard", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=home, steps=(early, broken, later))
    first = engine.handle(plan_at(tmp_path))
    assert [o.status for o in first.observations] == ["success", "failure"]
    assert later.observed == 0
    assert "SECRET_SENTINEL_71" not in first.model_dump_json()
    assert "SECRET_SENTINEL_71" not in (home / "setup.json").read_text()
    broken.failure = None
    resumed = engine.handle(plan_at(tmp_path))
    assert all(o.status == "success" for o in resumed.observations)
    assert early.applied == 1
    assert broken.applied == 2
    assert later.applied == 1


def test_action_required_preserves_human_boundary(tmp_path: Path) -> None:
    step = MarkerStep("claude", tmp_path / "effects")
    step.pending = True
    result = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan_at(tmp_path)
    )
    assert result.observations[-1].status == "action_required"
    assert step.applied == 0


def test_competing_run_refused_before_observation(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    step = MarkerStep("workspace", tmp_path / "effects")
    with (home / "setup.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="concurrent"):
            HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(
                plan_at(tmp_path)
            )
    assert step.observed == 0


def test_legacy_state_imported_once_and_never_used_as_proof(tmp_path: Path) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    (home / "setup.json").write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace with spaces"),
                "install_path": "local",
            }
        )
    )
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=home, steps=(step,))
    migrated = engine.handle(plan_at(tmp_path))
    assert migrated.imported_legacy
    assert migrated.schema_version == 2
    assert step.applied == 1
    again = engine.handle(plan_at(tmp_path))
    assert not again.imported_legacy
    assert step.applied == 1


@pytest.mark.parametrize(
    "raw",
    [
        "broken json",
        '{"schema_version":999}',
        '{"workspace":3}',
        '{"workspace":"/valid","install_path":"local","api_key":"SECRET_SENTINEL_71"}',
    ],
)
def test_unreadable_state_preserved_before_effects(tmp_path: Path, raw: str) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    state = home / "setup.json"
    state.write_text(raw)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(ValueError, match="state"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert state.read_text() == raw
    assert step.observed == 0


def test_atomic_write_failure_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    state = home / "setup.json"
    original = json.dumps(
        {"workspace": str(tmp_path / "workspace"), "install_path": "local"}
    )
    state.write_text(original)

    def refuse_replace(*args: object, **kwargs: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr("os.replace", refuse_replace)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(OSError, match="injected replacement failure"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert state.read_text() == original
    assert sorted(p.name for p in home.iterdir()) == ["setup.json", "setup.lock"]


def test_plan_rejects_secrets_before_effects(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        plan_at(tmp_path, api_key="SECRET_SENTINEL_71")
    with pytest.raises(ValidationError):
        plan_at(
            tmp_path,
            providers=(
                {
                    "provider": "openai",
                    "models": ("m1",),
                    "api_key": "SECRET_SENTINEL_71",
                },
            ),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:SECRET_SENTINEL_71@localhost:8000",
        "https://localhost:8000/?api_key=SECRET_SENTINEL_71",
        "https://localhost:8000/#SECRET_SENTINEL_71",
    ],
)
def test_plan_rejects_credentials_in_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValidationError):
        plan_at(
            tmp_path, providers=({"provider": "local", "models": ("m1",), "url": url},)
        )


def test_plan_rejects_incomplete_and_conflicting_choices(tmp_path: Path) -> None:
    for fields in (
        {"providers": ({"provider": "local", "models": ("m1",)},)},
        {"providers": ({"provider": "openai", "models": ()},)},
        {"components": (" ",)},
        {"components": ("delegation", "delegation")},
    ):
        with pytest.raises(ValidationError):
            plan_at(tmp_path, **fields)


def test_relative_workspace_and_model_order_are_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = ModelSetupPlan(workspace=Path("relative folder"))
    assert plan.workspace == tmp_path / "relative folder"
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    a = plan_at(tmp_path, providers=({"provider": "openai", "models": ("m1", "m2")},))
    b = plan_at(tmp_path, providers=({"provider": "openai", "models": ("m2", "m1")},))
    assert engine.handle(a).desired_digest == engine.handle(a).desired_digest
    assert engine.handle(a).desired_digest != engine.handle(b).desired_digest


@pytest.mark.parametrize("target", ["setup.json", "setup.lock"])
def test_symlink_state_targets_refused_without_touching_foreign_file(
    tmp_path: Path, target: str
) -> None:
    home = tmp_path / "onex"
    home.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_text("preserve me")
    (home / target).symlink_to(foreign)
    step = MarkerStep("workspace", tmp_path / "effects")
    with pytest.raises(ValueError, match="state"):
        HandlerSetupWorkflow(onex_home=home, steps=(step,)).handle(plan_at(tmp_path))
    assert foreign.read_text() == "preserve me"
    assert step.observed == 0


def test_partial_failure_record_does_not_claim_no_change(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    step.failure = "possibly after mutation"
    result = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,)).handle(
        plan_at(tmp_path)
    )
    assert result.observations[-1].changed


def test_docker_and_native_configuration_distinct(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    engine = HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=(step,))
    native = engine.handle(plan_at(tmp_path))
    docker = engine.handle(plan_at(tmp_path, components=("delegation", "docker")))
    assert native.install_path == "local"
    assert docker.install_path == "docker"
    assert native.desired_digest != docker.desired_digest


def test_no_empty_or_ambiguous_workflow(tmp_path: Path) -> None:
    step = MarkerStep("workspace", tmp_path / "effects")
    for steps in ((), (step, step), (MarkerStep("", tmp_path),)):
        with pytest.raises(ValueError, match="Setup requires"):
            HandlerSetupWorkflow(onex_home=tmp_path / "onex", steps=steps)
    assert not (tmp_path / "onex").exists()
